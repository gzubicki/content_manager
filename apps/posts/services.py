import base64
import json
import logging
import os
import shutil
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import httpx
from openai import (
    OpenAI,
    RateLimitError,
    APIError,
    APIConnectionError,
    Timeout,
    BadRequestError,
)

from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from telegram import Bot
from rapidfuzz import fuzz
from .models import Post, PostMedia, Channel
from dateutil import tz
from typing import Any


logger = logging.getLogger(__name__)


def _bot_for(channel: Channel):
    token = (channel.bot_token or "").strip()
    if not token:
        return None
    return Bot(token=token)

_oai = None
_IMAGE_SUPPORTS_B64: bool = True
def _client():
    global _oai
    if _oai is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("Brak OPENAI_API_KEY – drafty wymagają GPT.")
        _oai = OpenAI(api_key=key, max_retries=0, timeout=30)
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
        prompt = str(entry.get("prompt", "") or "").strip()
        title = str(entry.get("title", "") or "").strip()
        has_spoiler_value = entry.get("has_spoiler", entry.get("spoiler"))
        has_spoiler = bool(has_spoiler_value) if has_spoiler_value is not None else False
        source = str(entry.get("source", "") or "").strip().lower()
        if not source:
            source = "article" if url else "generate"
        normalised.append(
            {
                "type": media_type,
                "url": url,
                "prompt": prompt,
                "title": title,
                "source": source,
                "has_spoiler": has_spoiler,
            }
        )
    if not normalised:
        normalised.append(
            {
                "type": "photo",
                "url": "",
                "prompt": fallback_prompt,
                "title": "Ilustracja tematu wpisu",
                "source": "generate",
                "has_spoiler": False,
            }
        )
    has_photo = any(item["type"] == "photo" for item in normalised)
    if not has_photo:
        normalised.insert(
            0,
            {
                "type": "photo",
                "url": "",
                "prompt": fallback_prompt,
                "title": "Ilustracja tematu wpisu",
                "source": "generate",
                "has_spoiler": False,
            },
        )
    for item in normalised:
        if item["type"] == "photo" and not item["url"] and not item["prompt"]:
            item["prompt"] = fallback_prompt
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
        kwargs: dict[str, Any] = {
            "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
            "temperature": float(os.getenv("OPENAI_TEMPERATURE", 0.3)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        r = cli.chat.completions.create(**kwargs)
        return r.choices[0].message.content.strip()
    except RateLimitError as e:
        # twarde „insufficient_quota” – nie retry’ujemy, zwracamy None
        if "insufficient_quota" in str(e):
            return None
        raise
    except (APIError, APIConnectionError, Timeout):
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
        "- media: lista 1-3 obiektów opisujących multimedia do posta.",
        "Każdy obiekt media powinien mieć pola: type (photo/video/doc), title (krótki opis),",
        "source (article/generate/external), opcjonalnie url (gdy istnieje źródło) oraz",
        "opcjonalnie prompt (opis do wygenerowania grafiki).",
        "Zawsze dodaj przynajmniej jeden element typu photo. Jeśli masz podany URL artykułu",
        "albo adres grafiki, użyj go w polu url i ustaw source=article.",
        "Jeżeli nie ma zdjęcia, przygotuj realistyczny prompt opisujący scenę pasującą",
        "do tematu (np. myśliwiec F-16 dla wiadomości o rakietach).",
        "Dla wideo/dokumentów zawsze podawaj pełny url.",
    ]
    if channel.max_chars:
        instructions.append(
            "Długość odpowiedzi musi mieścić się w limicie znaków opisanym w systemowym promptcie."
        )
    instructions.append(
        "Uwzględnij wymagania dotyczące emoji, stopki i zakazów opisane w systemowym promptcie."
    )
    if article_context:
        instructions.append("Korzystaj z poniższych danych artykułu:")
        instructions.append(article_context)
    usr = "\n".join(instructions)
    raw = gpt_generate_text(
        sys,
        usr,
        response_format={"type": "json_object"},
    )
    if raw is None:
        return None
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


def _extract_openai_error_param(error: BadRequestError) -> str | None:
    """Return the parameter that caused a BadRequestError, if present."""

    param = getattr(error, "param", None)
    if param:
        return str(param)
    body = getattr(error, "body", None)
    body_data: Any | None = None
    if isinstance(body, dict):
        body_data = body
    elif isinstance(body, (str, bytes)):
        try:
            body_data = json.loads(body)
        except Exception:
            body_data = None
    if isinstance(body_data, dict):
        nested = body_data.get("error")
        if isinstance(nested, dict):
            param = nested.get("param")
            if param:
                return str(param)
    response = getattr(error, "response", None)
    if response is not None:
        try:
            data = response.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            nested = data.get("error")
            if isinstance(nested, dict):
                param = nested.get("param")
                if param:
                    return str(param)
        try:
            text = response.text
        except Exception:
            text = None
        if isinstance(text, str) and "response_format" in text:
            return "response_format"
    message = getattr(error, "message", None)
    if isinstance(message, str) and "response_format" in message:
        return "response_format"
    if "response_format" in str(error):
        return "response_format"
    return None


def _generate_photo_for_media(pm: PostMedia, prompt: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return pm.cache_path or ""
    client = _client()
    model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    size = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")
    quality = os.getenv("OPENAI_IMAGE_QUALITY", "standard")
    request_kwargs = {
        "model": model,
        "prompt": prompt,
    }
    if size:
        request_kwargs["size"] = size
    if quality:
        request_kwargs["quality"] = quality

    global _IMAGE_SUPPORTS_B64

    response = None
    if _IMAGE_SUPPORTS_B64:
        try:
            response = client.images.generate(
                **request_kwargs,
                response_format="b64_json",
            )
        except BadRequestError as exc:
            if _extract_openai_error_param(exc) != "response_format":
                raise
            _IMAGE_SUPPORTS_B64 = False
            response = client.images.generate(**request_kwargs)
    if response is None:
        response = client.images.generate(**request_kwargs)
    if not response.data:
        return pm.cache_path or ""
    first_item = response.data[0]
    image_data = getattr(first_item, "b64_json", None)
    if isinstance(first_item, dict) and not image_data:
        image_data = first_item.get("b64_json")
    binary_content: bytes | None = None
    if image_data:
        binary_content = base64.b64decode(image_data)
    else:
        image_url = getattr(first_item, "url", None)
        if isinstance(first_item, dict) and not image_url:
            image_url = first_item.get("url")
        if not image_url:
            return pm.cache_path or ""
        download = httpx.get(image_url, timeout=30, follow_redirects=True)
        download.raise_for_status()
        binary_content = download.content
    if binary_content is None:
        return pm.cache_path or ""
    media_root = Path(settings.MEDIA_ROOT)
    cache_dir = media_root / "cache"
    os.makedirs(cache_dir, exist_ok=True)
    fname = cache_dir / f"{pm.id}.png"
    with open(fname, "wb") as fh:
        fh.write(binary_content)
    pm.cache_path = fname.as_posix()
    pm.expires_at = _media_expiry_deadline()
    pm.save(update_fields=["cache_path", "expires_at"])
    return pm.cache_path


def attach_media_from_payload(post: Post, media_payload: list[dict[str, Any]]):
    post.media.all().delete()
    for idx, item in enumerate(media_payload):
        media_type = str(item.get("type", "photo") or "photo").strip().lower()
        if media_type == "image":
            media_type = "photo"
        if media_type not in {"photo", "video", "doc"}:
            continue
        url = str(item.get("url", "") or "").strip()
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
        if url:
            try:
                cache_media(pm)
            except Exception:
                logger.exception("Nie udało się pobrać medium %s dla posta %s", pm.id, post.id)
        elif media_type == "photo" and item.get("prompt"):
            try:
                _generate_photo_for_media(pm, item["prompt"])
            except Exception:
                logger.exception("Nie udało się wygenerować grafiki dla posta %s", post.id)


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
    if now.minute % step != 0 or now.second or now.microsecond:
        candidate += timezone.timedelta(minutes=step)

    if candidate < start:
        candidate = start
    if candidate > end:
        candidate = start + timezone.timedelta(days=1)

    used_slots = {
        timezone.localtime(dt, tz_waw)
        for dt in channel.posts.filter(
            status__in=["APPROVED", "SCHEDULED"],
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
    if pm.cache_path and os.path.exists(pm.cache_path):
        return pm.cache_path
    url = (pm.source_url or "").strip()
    if not url: return ""
    media_root = Path(settings.MEDIA_ROOT)
    cache_dir = media_root / "cache"
    os.makedirs(cache_dir, exist_ok=True)
    parsed = urlparse(url)
    path = parsed.path or ""
    ext = os.path.splitext(path)[-1] or ".bin"
    fname = cache_dir / f"{pm.id}{ext}"

    if parsed.scheme in ("http", "https"):
        with httpx.stream("GET", url, timeout=30) as r:
            r.raise_for_status()
            with open(fname, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    else:
        src = path if parsed.scheme == "file" else url
        if not os.path.isabs(src):
            candidate = (media_root / src).resolve()
            if candidate.exists():
                src = candidate.as_posix()
        if not os.path.exists(src):
            return pm.cache_path or ""
        shutil.copyfile(src, fname)

    pm.cache_path = fname.as_posix()
    pm.expires_at = timezone.now() + timedelta(days=int(os.getenv("MEDIA_CACHE_TTL_DAYS", 7)))
    pm.save()
    return pm.cache_path
