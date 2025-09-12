import os, httpx, asyncio
from tenacity import retry, wait_exponential_jitter, stop_after_attempt
from openai import OpenAI
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from telegram import Bot
from rapidfuzz import fuzz
from .models import Post, PostMedia, Channel
from dateutil import tz

def _bot_for(channel: Channel):
    token = channel.bot_token or settings.TG_BOT_TOKEN
    return Bot(token=token)

_oai = None
def _client():
    global _oai
    if _oai is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("Brak OPENAI_API_KEY – drafty wymagają GPT.")
        _oai = OpenAI(api_key=key)
    return _oai

def _channel_system_prompt(ch: Channel) -> str:
    return (ch.style_prompt or
            "Piszesz WYŁĄCZNIE po polsku. 1–3 akapity, ⚡️ lead, bez linków, stopka w 2 liniach.")

@retry(wait=wait_exponential_jitter(1, 8), stop=stop_after_attempt(3))
def gpt_generate_text(system_prompt: str, user_prompt: str) -> str:
    cli = _client()
    resp = cli.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        temperature=float(os.getenv("OPENAI_TEMPERATURE", 0.3)),
        messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],
    )
    return resp.choices[0].message.content.strip()

def gpt_new_draft(channel: Channel) -> str:
    sys = _channel_system_prompt(channel)
    usr = ("Wygeneruj JEDEN gotowy wpis zgodnie z zasadami. "
           "Zawrzyj stopkę identyczną jak w kanale. Nie dodawaj linków.")
    return gpt_generate_text(sys, usr)

def gpt_rewrite_text(channel: Channel, text: str, editor_prompt: str) -> str:
    sys = _channel_system_prompt(channel)
    usr = ("Przepisz poniższy tekst zgodnie z zasadami i wytycznymi edytora. "
           "Zachowaj polski język, lead ⚡️, bez linków; nie usuwaj stopki kanału.\n\n"
           f"[Wytyczne edytora]: {editor_prompt}\n\n[Tekst]:\n{text}")
    return gpt_generate_text(sys, usr)

def ensure_min_drafts(channel: Channel):
    need = channel.draft_target_count - channel.posts.filter(status="DRAFT").count()
    created = 0
    for _ in range(max(0, need)):
        text = gpt_new_draft(channel)
        Post.objects.create(channel=channel, text=text, status="DRAFT", origin="gpt")
        created += 1
    return created

def compute_dupe(post: Post) -> float:
    texts = Post.objects.filter(status="PUBLISHED").order_by("-id").values_list("text", flat=True)[:300]
    if not texts: return 0.0
    return max(fuzz.token_set_ratio(post.text, t)/100.0 for t in texts)

def next_auto_slot(channel: Channel, dt=None):
    tz_waw = tz.gettz("Europe/Warsaw")
    now = timezone.now().astimezone(tz_waw) if dt is None else dt.astimezone(tz_waw)
    step = channel.slot_step_min
    start = now.replace(hour=channel.slot_start_hour, minute=0, second=0, microsecond=0)
    end = now.replace(hour=channel.slot_end_hour, minute=channel.slot_end_minute, second=0, microsecond=0)
    minute = 0 if now.minute <= 0 else (30 if now.minute <= 30 else 60)
    base = now.replace(minute=0, second=0, microsecond=0)
    candidate = base if now.minute == 0 else base + timezone.timedelta(minutes=minute)
    if candidate < start: candidate = start
    if candidate > end: candidate = start + timezone.timedelta(days=1)
    used = set(channel.posts.filter(status__in=["APPROVED","SCHEDULED"]).values_list("scheduled_at", flat=True))
    while candidate in used:
        candidate += timezone.timedelta(minutes=step)
        if candidate.time() > end.time():
            candidate = start + timezone.timedelta(days=1)
    return candidate

def assign_auto_slot(post: Post):
    if post.schedule_mode == "MANUAL": return
    post.scheduled_at = next_auto_slot(post.channel)
    post.status = "SCHEDULED"
    post.save()

def purge_cache():
    for pm in PostMedia.objects.filter(expires_at__lt=timezone.now()):
        try:
            if pm.cache_path and os.path.exists(pm.cache_path):
                os.remove(pm.cache_path)
        finally:
            pm.cache_path = ""; pm.save()
            
def cache_media(pm: PostMedia):
    if pm.cache_path: return pm.cache_path
    url = pm.source_url
    if not url: return ""
    cache_dir = settings.MEDIA_ROOT / "cache"
    os.makedirs(cache_dir, exist_ok=True)
    ext = os.path.splitext(url)[-1] or ".bin"
    fname = (cache_dir / f"{pm.id}{ext}").as_posix()
    with httpx.stream("GET", url, timeout=30) as r:
        r.raise_for_status()
        with open(fname, "wb") as f:
            for chunk in r.iter_bytes(): f.write(chunk)
    pm.cache_path = fname
    pm.expires_at = timezone.now() + timedelta(days=int(os.getenv("MEDIA_CACHE_TTL_DAYS", 7)))
    pm.save(); return fname