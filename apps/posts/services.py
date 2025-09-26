import json
import logging
import mimetypes
import os
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import httpx
from openai import (
    OpenAI,
    RateLimitError,
    APIError,
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
)

from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from telegram import Bot
from rapidfuzz import fuzz
from .models import Channel, Post, PostMedia
from dateutil import tz
from typing import Any


logger = logging.getLogger(__name__)


def _bot_for(channel: Channel):
    token = (channel.bot_token or "").strip()
    if not token:
        return None
    return Bot(token=token)

_oai = None


def _client():
    global _oai
    if _oai is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("Brak OPENAI_API_KEY – drafty wymagają GPT.")
        timeout_s = float(os.getenv("OPENAI_TIMEOUT", 60))
        max_retries = int(os.getenv("OPENAI_MAX_RETRIES", 0))
        _oai = OpenAI(api_key=key, max_retries=max_retries, timeout=timeout_s)
    return _oai

def _channel_constraints_prompt(ch: Channel) -> str:
    rules: list[str] = []
    language = (ch.language or "").strip()
    if language:
        rules.append(f"Piszesz w języku: {language}.")
    rules.append(f"Limit długości tekstu: maksymalnie {ch.max_chars} znaków.")
    rules.append(
        f"Liczba emoji w treści: co najmniej {ch.emoji_min}, najwyżej {ch.emoji_max}."
    )
    footer = (ch.footer_text or "").strip()
    if footer:
        rules.append("Stopka kanału:")
        rules.append(footer)
    if ch.no_links_in_text:
        rules.append("Nie dodawaj linków w treści posta.")
    return "\n".join(rules)


def _channel_system_prompt(ch: Channel) -> str:
    base = (ch.style_prompt or "").strip()
    rules = _channel_constraints_prompt(ch).strip()
    if rules:
        return f"{base}\n\nWytyczne kanału:\n{rules}"
    return base


def _strip_code_fence(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.splitlines() if not line.startswith("```")]
        return "\n".join(lines).strip()
    return cleaned


def _default_image_prompt(post_text: str) -> str:
    snippet = textwrap.shorten(" ".join(post_text.split()), width=220, placeholder="…")
    return (
        "Fotorealistyczne zdjęcie ilustrujące temat wpisu: "
        f"{snippet}. Reporterskie ujęcie, realistyczne kolory, brak napisów."
    )


def _normalise_media_payload(media: Any, fallback_prompt: str) -> list[dict[str, Any]]:
    supported = {"photo", "video", "doc"}
    items = media or []
    if isinstance(items, dict):
        items = [items]
    normalised: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        media_type = str(entry.get("type", "")).strip().lower() or "photo"
        if media_type == "image":
            media_type = "photo"
        if media_type not in supported:
            continue
        url = str(entry.get("url", "") or "").strip()
        if not url:
            logger.info("Pomijam media bez url: %s", entry)
            continue
        title = str(entry.get("title", "") or "").strip()
        has_spoiler_value = entry.get("has_spoiler", entry.get("spoiler"))
        has_spoiler = bool(has_spoiler_value) if has_spoiler_value is not None else False
        source = str(entry.get("source", "") or "").strip().lower() or "external"
        normalised.append(
            {
                "type": media_type,
                "url": url,
                "title": title,
                "source": source,
                "has_spoiler": has_spoiler,
            }
        )
    return normalised[:5]


def _parse_gpt_payload(raw: str) -> dict[str, Any] | None:
    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("GPT zwrócił niepoprawny JSON: %s", raw)
        return None
    if not isinstance(data, dict):
        return None
    text = str(data.get("post_text", "") or "").strip()
    if not text:
        return None
    media = _normalise_media_payload(data.get("media"), _default_image_prompt(text))
    return {"post_text": text, "media": media, "raw_response": cleaned}


def gpt_generate_text(system_prompt: str, user_prompt: str, *, response_format: dict[str, Any] | None = None) -> str | None:
    cli = _client()
    try:
        model = os.getenv("OPENAI_MODEL", "gpt-4.1")
        temperature = float(os.getenv("OPENAI_TEMPERATURE", 0.2))

        use_tools = response_format is None
        if use_tools:
            try:
                response = cli.responses.create(
                    model=model,
                    temperature=temperature,
                    input=[
                        {
                            "role": "system",
                            "content": [{"type": "text", "text": system_prompt}],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": user_prompt}],
                        },
                    ],
                    tools=[
                        {"type": "web_search"},
                        {"type": "image_generation"},
                    ],
                )
                text_chunks: list[str] = []
                output_items = getattr(response, "output", None) or []
                for item in output_items:
                    content = getattr(item, "content", None) or []
                    for part in content:
                        text = getattr(part, "text", None)
                        if text:
                            text_chunks.append(text)
                if not text_chunks:
                    fallback_text = getattr(response, "output_text", None)
                    if isinstance(fallback_text, str) and fallback_text.strip():
                        text_chunks.append(fallback_text.strip())
                combined = "\n".join(chunk.strip() for chunk in text_chunks if chunk).strip()
                if combined:
                    return combined
            except BadRequestError as exc:
                error_param = getattr(exc, "param", "") or ""
                if "tools" in error_param or "tools" in str(exc):
                    logger.warning(
                        "Model %s odrzucił narzędzia (%s) – fallback do zapytania bez tools.",
                        model,
                        error_param or exc,
                    )
                else:
                    raise

        chat_kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if response_format is not None:
            chat_kwargs["response_format"] = response_format
        chat_response = cli.chat.completions.create(**chat_kwargs)
        return chat_response.choices[0].message.content.strip()
    except RateLimitError as e:
        # twarde „insufficient_quota” – nie retry’ujemy, zwracamy None
        if "insufficient_quota" in str(e):
            return None
        raise
    except (APIError, APIConnectionError, APITimeoutError):
        # pozwól Celery autoretry
        raise

def gpt_new_draft(channel: Channel) -> dict[str, Any] | None:
    return gpt_generate_post_payload(channel)


def gpt_generate_post_payload(channel: Channel, article: dict[str, Any] | None = None) -> dict[str, Any] | None:
    sys = _channel_system_prompt(channel)
    article_context = ""
    if article:
        article_bits = []
        title = str(article.get("title", "") or "").strip()
        if title:
            article_bits.append(f"Tytuł artykułu: {title}")
        summary = str(article.get("summary", "") or "").strip()
        if summary:
            article_bits.append(f"Podsumowanie artykułu: {summary}")
        lead = str(article.get("lead", "") or "").strip()
        if lead:
            article_bits.append(f"Lead: {lead}")
        image_url = str(article.get("image_url", "") or "").strip()
        if image_url:
            article_bits.append(f"Preferowane zdjęcie: {image_url}")
        url = str(article.get("url", "") or "").strip()
        if url:
            article_bits.append(f"Źródło: {url}")
        if article_bits:
            article_context = "\n".join(article_bits)
    instructions = [
        "Zwróć dokładnie jeden obiekt JSON zawierający pola:",
        "- post_text: gotowy tekst posta zgodny z zasadami kanału;",
        "- media: lista 1-5 obiektów opisujących multimedia do posta.",
        "Każdy obiekt media MUSI mieć pola: type (photo/video/doc), title (krótki opis)",
        "oraz source (article/generate/external) i url będący bezpośrednim linkiem do pliku.",
        "Adres url ma prowadzić prosto do zasobu (np. .jpg, .png, .mp4, .pdf) i być publicznie dostępny.",
        "Wykożystaj zdjęcia z artykułów, znajdź pasujące zdjęcie w sieci pasujące do treści, jeśli nie masz pliku, wygeneruj.",
        "Zawsze dodaj przynajmniej jeden element typu photo/video",
    ]
    if channel.max_chars:
        instructions.append(
            "Długość odpowiedzi musi mieścić się w limicie znaków opisanym w systemowym promptcie."
        )

    if article_context:
        instructions.append("Korzystaj z poniższych danych artykułu:")
        instructions.append(article_context)
    usr = "\n".join(instructions)
    logger.info(
        "GPT draft request (channel=%s)\nSYSTEM:\n%s\nUSER:\n%s",
        channel.id,
        sys,
        usr,
    )
    raw = gpt_generate_text(
        sys,
        usr,
        response_format={"type": "json_object"},
    )
    if raw is None:
        return None
    logger.info(
        "GPT draft response (channel=%s): %s",
        channel.id,
        raw,
    )
    return _parse_gpt_payload(raw)

def gpt_rewrite_text(channel: Channel, text: str, editor_prompt: str) -> str:
    sys = _channel_system_prompt(channel)
    usr = (
        "Przepisz poniższy tekst zgodnie z zasadami i wytycznymi edytora. "
        "Zachowaj charakter kanału, wymagania dotyczące długości, emoji oraz stopki opisane w systemowym promptcie."
        f"\n\n[Wytyczne edytora]: {editor_prompt}\n\n[Tekst]:\n{text}"
    )
    return gpt_generate_text(sys, usr)


def _media_expiry_deadline():
    return timezone.now() + timedelta(days=int(os.getenv("MEDIA_CACHE_TTL_DAYS", 7)))


def attach_media_from_payload(post: Post, media_payload: list[dict[str, Any]]):
    post.media.all().delete()
    for idx, item in enumerate(media_payload):
        media_type = str(item.get("type", "photo") or "photo").strip().lower()
        if media_type == "image":
            media_type = "photo"
        if media_type not in {"photo", "video", "doc"}:
            continue
        url = str(item.get("url", "") or "").strip()
        if not url:
            logger.info("Pomijam media typu %s bez url dla posta %s", media_type, post.id)
            continue
        has_spoiler = item.get("has_spoiler")
        if has_spoiler is None and media_type == "photo":
            has_spoiler = bool(getattr(post.channel, "auto_blur_default", False))
        else:
            has_spoiler = bool(has_spoiler)
        pm = PostMedia.objects.create(
            post=post,
            type=media_type,
            source_url=url,
            order=idx,
            has_spoiler=has_spoiler,
        )
        try:
            cache_path = cache_media(pm)
        except Exception:
            logger.exception("Nie udało się pobrać medium %s dla posta %s", pm.id, post.id)
            pm.delete()
            continue
        if not cache_path:
            logger.info(
                "Pomijam medium %s dla posta %s – brak cache po pobraniu (%s)",
                pm.id,
                post.id,
                url,
            )
            pm.delete()


def create_post_from_payload(channel: Channel, payload: dict[str, Any]) -> Post:
    text = str(payload.get("post_text", "") or "").strip()
    if not text:
        raise ValueError("Brak treści posta w odpowiedzi GPT")
    raw_payload = payload.get("raw_response")
    if raw_payload is None:
        raw_payload = json.dumps(payload, ensure_ascii=False)
    post = Post.objects.create(
        channel=channel,
        text=text,
        status="DRAFT",
        origin="gpt",
        generated_prompt=raw_payload,
    )
    media_items = payload.get("media") or []
    if isinstance(media_items, list):
        attach_media_from_payload(post, media_items)
    return post


def ensure_min_drafts(channel: Channel):
    need = channel.draft_target_count - channel.posts.filter(status="DRAFT").count()
    created = 0
    for _ in range(max(0, need)):
        payload = gpt_new_draft(channel)
        if payload is None:
            break
        create_post_from_payload(channel, payload)
        created += 1
    return created

def compute_dupe(post: Post) -> float:
    texts = Post.objects.filter(status="PUBLISHED").order_by("-id").values_list("text", flat=True)[:300]
    if not texts: return 0.0
    return max(fuzz.token_set_ratio(post.text, t)/100.0 for t in texts)

def next_auto_slot(channel: Channel, dt=None):
    tz_waw = tz.gettz("Europe/Warsaw")
    now = timezone.now().astimezone(tz_waw) if dt is None else dt.astimezone(tz_waw)
    step = max(channel.slot_step_min, 1)
    start = now.replace(hour=channel.slot_start_hour, minute=0, second=0, microsecond=0)
    end = now.replace(hour=channel.slot_end_hour, minute=channel.slot_end_minute, second=0, microsecond=0)

    minute_block = (now.minute // step) * step
    candidate = now.replace(minute=minute_block, second=0, microsecond=0)
    if candidate <= now:
        candidate += timezone.timedelta(minutes=step)

    if candidate < start:
        candidate = start
    if candidate > end:
        candidate = start + timezone.timedelta(days=1)

    used_slots = {
        timezone.localtime(dt, tz_waw)
        for dt in channel.posts.filter(
            status__in=[Post.Status.APPROVED, Post.Status.SCHEDULED],
            scheduled_at__isnull=False
        ).values_list("scheduled_at", flat=True)
    }
    safety_counter = 0
    while candidate in used_slots:
        candidate += timezone.timedelta(minutes=step)
        safety_counter += 1
        if candidate.time() > end.time() or safety_counter > (24 * 60 // step) + 1:
            candidate = start + timezone.timedelta(days=1)
            safety_counter = 0
    return candidate

def assign_auto_slot(post: Post):
    if post.schedule_mode == "MANUAL": return
    post.scheduled_at = next_auto_slot(post.channel)
    post.dupe_score = compute_dupe(post)
    post.status = Post.Status.SCHEDULED
    post.save()


def approve_post(post: Post, user=None):
    """Mark a draft as approved and assign the next automatic publication slot."""

    post.status = Post.Status.APPROVED
    if user and getattr(user, "is_authenticated", False):
        post.approved_by = user
    post.scheduled_at = next_auto_slot(post.channel)
    post.dupe_score = compute_dupe(post)
    post.save()
    return post

def purge_cache():
    for pm in PostMedia.objects.filter(expires_at__lt=timezone.now()):
        try:
            if pm.cache_path and os.path.exists(pm.cache_path):
                os.remove(pm.cache_path)
        finally:
            pm.cache_path = ""; pm.save()
            
def cache_media(pm: PostMedia):
    if pm.cache_path and os.path.exists(pm.cache_path):
        return pm.cache_path
    url = (pm.source_url or "").strip()
    if not url: return ""
    media_root = Path(settings.MEDIA_ROOT)
    cache_dir = media_root / "cache"
    os.makedirs(cache_dir, exist_ok=True)

    parsed = urlparse(url)
    path = parsed.path or ""
    ext = os.path.splitext(path)[-1].lower()
    content: bytes | None = None

    if parsed.scheme in ("http", "https"):
        timeout_s = float(os.getenv("MEDIA_DOWNLOAD_TIMEOUT", 30))
        try:
            response = httpx.get(url, timeout=timeout_s, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HTTP %s przy pobieraniu %s dla media %s",
                exc.response.status_code,
                url,
                pm.id,
            )
            return pm.cache_path or ""
        except httpx.RequestError as exc:
            logger.warning(
                "Błąd sieci przy pobieraniu %s dla media %s: %s",
                url,
                pm.id,
                exc,
            )
            return pm.cache_path or ""

        content = response.content
        if not content:
            logger.warning("Pusty plik zwrócony z %s dla media %s", url, pm.id)
            return pm.cache_path or ""

        if not ext:
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            if content_type:
                guessed = mimetypes.guess_extension(content_type)
                if guessed:
                    ext = guessed
        if not ext:
            ext = ".bin"
    else:
        src = path if parsed.scheme == "file" else url
        if not os.path.isabs(src):
            candidate = (media_root / src).resolve()
            if candidate.exists():
                src = candidate.as_posix()
        if not os.path.exists(src):
            return pm.cache_path or ""
        try:
            with open(src, "rb") as fh:
                content = fh.read()
        except Exception:
            logger.exception("Nie udało się odczytać pliku %s dla media %s", src, pm.id)
            return pm.cache_path or ""
        if not ext:
            ext = os.path.splitext(src)[-1] or ".bin"

    fname = cache_dir / f"{pm.id}{ext}"
    try:
        with open(fname, "wb") as fh:
            fh.write(content)
    except Exception:
        logger.exception("Nie udało się zapisać pliku cache %s dla media %s", fname, pm.id)
        return pm.cache_path or ""

    pm.cache_path = fname.as_posix()
    pm.expires_at = timezone.now() + timedelta(days=int(os.getenv("MEDIA_CACHE_TTL_DAYS", 7)))
    pm.save()
    return pm.cache_path
