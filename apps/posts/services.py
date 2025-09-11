import os, httpx
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from telegram import Bot
from rapidfuzz import fuzz
from .models import Post, PostMedia, Channel

def _bot_for(channel: Channel):
    token = channel.bot_token or settings.TG_BOT_TOKEN
    return Bot(token=token)

def ensure_min_drafts(channel: Channel):
    need = channel.draft_target_count - channel.posts.filter(status="DRAFT").count()
    created = 0
    for _ in range(max(0, need)):
        text = "⚡️ Nowy draft...\n\nTreść…\n\n" + channel.footer_text
        Post.objects.create(channel=channel, text=text, status="DRAFT")
        created += 1
    return created

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

def purge_cache():
    for pm in PostMedia.objects.filter(expires_at__lt=timezone.now()):
        try:
            if pm.cache_path and os.path.exists(pm.cache_path):
                os.remove(pm.cache_path)
        finally:
            pm.cache_path = ""; pm.save()

from dateutil import tz
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
