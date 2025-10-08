import base64
import json
import logging
import mimetypes
import os
import textwrap
import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote

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
from typing import Any, Optional


logger = logging.getLogger(__name__)


def _bot_for(channel: Channel):
    token = (channel.bot_token or "").strip()
    if not token:
        return None
    return Bot(token=token)

_oai = None
_OPENAI_SEED: Optional[int] = None

_SUPPORTED_MEDIA_TYPES = {"photo", "video", "doc"}
_IDENTIFIER_KEYS = (
    "tweet_id",
    "tg_post_url",
    "message_id",
    "chat_id",
    "video_id",
    "record_id",
    "media_id",
    "post_id",
    "story_id",
    "permalink",
    "source_locator",
)
_PLACEHOLDER_IDENTIFIER_VALUES = {
    "tg_post_url",
    "tweet_id",
    "message_id",
    "chat_id",
    "video_id",
    "record_id",
    "media_id",
    "post_id",
    "story_id",
    "permalink",
    "external_id",
    "source_locator",
}
_IDENTIFIER_KEY_ALIASES = {
    "identyfikator": "identifier",
    "identyfikator źródła": "source_locator",
    "identyfikator zrodla": "source_locator",
    "source identifier": "source_locator",
    "source id": "source_locator",
    "id źródła": "source_locator",
    "id zrodla": "source_locator",
    "source locator": "source_locator",
}


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


def _openai_seed() -> Optional[int]:
    global _OPENAI_SEED
    if _OPENAI_SEED is not None:
        return _OPENAI_SEED
    raw = os.getenv("OPENAI_SEED", "").strip()
    if not raw:
        _OPENAI_SEED = None
        return None
    try:
        _OPENAI_SEED = int(raw)
    except ValueError:
        logger.warning("OPENAI_SEED musi być liczbą całkowitą – pomijam wartość %r", raw)
        _OPENAI_SEED = None
    return _OPENAI_SEED

def _channel_constraints_prompt(channel: Channel) -> str:
    rules: list[str] = []
    language = (channel.language or "").strip()
    if language:
        rules.append(f"Piszesz w języku: {language}.")

    if getattr(channel, "max_chars", None):
        rules.append(f"Limit długości tekstu: maksymalnie {channel.max_chars} znaków.")

    emoji_min = getattr(channel, "emoji_min", None)
    emoji_max = getattr(channel, "emoji_max", None)
    if emoji_min or emoji_max:
        low = emoji_min or 0
        high = emoji_max or 0
        rules.append(f"Liczba emoji w treści:od {low}, do {high}.")

    footer = (channel.footer_text or "").strip()
    if footer:
        rules.append("Stopka kanału:")
        rules.append(footer)

    if getattr(channel, "no_links_in_text", False):
        rules.append("Nie dodawaj linków w treści.")

    return "\n".join(rule for rule in rules if rule)


def _channel_system_prompt(channel: Channel) -> str:
    base = (channel.style_prompt or "").strip()
    rules = _channel_constraints_prompt(channel).strip()
    if rules:
        return f"{base}\n\nWytyczne kanału:\n{rules}"
    return base


def _article_context(article: dict[str, Any] | None) -> str:
    if not isinstance(article, dict) or not article:
        return ""

    bits: list[str] = []

    post_data = article.get("post")
    if isinstance(post_data, dict):
        raw_text = str(post_data.get("text", "") or "").strip()
        if raw_text:
            bits.append("Treść źródłowa:")
            bits.append(raw_text)

        headline = str(post_data.get("title", "") or "").strip()
        if headline:
            bits.insert(0, f"Tytuł: {headline}")

        summary = str(post_data.get("summary", "") or "").strip()
        if summary:
            bits.append("Streszczenie:")
            bits.append(summary)

    media_data = article.get("media")
    if isinstance(media_data, list) and media_data:
        bits.append("Media źródłowe:")
        for idx, item in enumerate(media_data, 1):
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("type", "") or "").strip()
            url = str(item.get("source_url") or item.get("url") or "").strip()
            caption = str(item.get("caption") or item.get("title") or "").strip()
            label = f"{idx}. {media_type or 'media'}"
            if caption and url:
                bits.append(f"{label}: {caption} – {url}")
            elif url:
                bits.append(f"{label}: {url}")
            elif caption:
                bits.append(f"{label}: {caption}")

    if bits:
        return "\n".join(bits)

    # Fallback to legacy keys for backwards compatibility
    fallback_mappings = (
        ("title", "Tytuł"),
        ("summary", "Podsumowanie"),
        ("lead", "Lead"),
        ("image_url", "Preferowane zdjęcie"),
        ("url", "Źródło"),
    )
    legacy_bits: list[str] = []
    for key, label in fallback_mappings:
        value = str(article.get(key, "") or "").strip()
        if value:
            legacy_bits.append(f"{label}: {value}")
    return "\n".join(legacy_bits)


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


def _guess_media_type_from_url(url: str, fallback: str) -> str:
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
        ext = Path(path).suffix.lower()
    except Exception:
        ext = ""
    if ext in IMAGE_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in DOC_EXTENSIONS:
        return "doc"
    return fallback


def _normalise_media_payload(media: Any, fallback_prompt: str) -> list[dict[str, Any]]:
    items = media or []
    if isinstance(items, (dict, str)):
        items = [items]

    normalised: list[dict[str, Any]] = []

    for raw in items:
        logger.debug("Processing media entry %s", raw)
        entry = raw if isinstance(raw, dict) else {"url": raw}
        if not isinstance(entry, dict):
            continue

        normalised_entry: dict[str, Any] = {}
        for key, value in entry.items():
            key_str = str(key).strip()
            alias = _IDENTIFIER_KEY_ALIASES.get(key_str.lower(), key_str)
            normalised_entry[alias] = value
        entry = normalised_entry

        media_type = _normalise_type(entry.get("type"))
        if media_type not in _SUPPORTED_MEDIA_TYPES:
            continue

        has_spoiler_value = entry.get("has_spoiler", entry.get("spoiler"))
        has_spoiler = bool(has_spoiler_value) if has_spoiler_value is not None else False
        caption = str(entry.get("caption") or entry.get("title") or "").strip()
        source_label = str(entry.get("source") or "").strip()
        resolver = str(
            entry.get("resolver")
            or entry.get("provider")
            or entry.get("source_type")
            or entry.get("source_name")
            or ""
        ).strip().lower()

        url_candidate = _first_url_from(entry)
        source_url = url_candidate.strip()

        reference: dict[str, str] = {}
        existing_reference = entry.get("reference") if isinstance(entry.get("reference"), dict) else {}
        for key, value in existing_reference.items():
            if value is None:
                continue
            reference[str(key)] = str(value).strip()

        for key in _IDENTIFIER_KEYS:
            value = entry.get(key)
            if value is None:
                continue
            val = str(value).strip()
            if val:
                reference[key] = val

        identifier = (
            entry.get("identifier")
            or entry.get("source_locator")
            or entry.get("identyfikator")
            or entry.get("id")
        )
        if isinstance(identifier, dict):
            name = str(
                identifier.get("name")
                or identifier.get("nazwa")
                or identifier.get("key")
                or identifier.get("type")
                or ""
            ).strip()
            value = str(
                identifier.get("value")
                or identifier.get("wartosc")
                or identifier.get("id")
                or identifier.get("identifier")
                or ""
            ).strip()
            if name and value:
                reference.setdefault(name, value)
            else:
                for key, value in identifier.items():
                    val = str(value or "").strip()
                    if val:
                        reference.setdefault(str(key), val)
        elif isinstance(identifier, str):
            ident_str = identifier.strip()
            if ident_str:
                if ident_str.startswith(("http://", "https://")):
                    if not source_url:
                        source_url = ident_str
                    if resolver == "telegram":
                        reference["tg_post_url"] = ident_str
                elif ident_str in entry and entry.get(ident_str):
                    reference[ident_str] = str(entry.get(ident_str)).strip()
                elif resolver == "telegram":
                    reference["message_id"] = ident_str
                elif resolver == "twitter":
                    reference["tweet_id"] = ident_str
                elif resolver == "instagram":
                    reference["shortcode"] = ident_str
                else:
                    reference["external_id"] = ident_str

        posted_at = str(entry.get("posted_at") or "").strip()
        if posted_at:
            reference.setdefault("posted_at", posted_at)

        if not resolver:
            if "tweet_id" in reference:
                resolver = "twitter"
            elif "tg_post_url" in reference or source_url.startswith("https://t.me/"):
                resolver = "telegram"
            elif "shortcode" in reference:
                resolver = "instagram"

        cleaned_reference: dict[str, str] = {}
        for key, value in reference.items():
            val = str(value or "").strip()
            if not val:
                continue
            lower_val = val.lower()
            if lower_val == key.lower():
                logger.warning(
                    "Pomijam placeholder identyfikatora %s=%s w media %s",
                    key,
                    val,
                    entry,
                )
                continue
            if lower_val in _PLACEHOLDER_IDENTIFIER_VALUES:
                logger.warning(
                    "Pomijam placeholder identyfikatora %s=%s w media %s",
                    key,
                    val,
                    entry,
                )
                continue
            cleaned_reference[key] = val
        reference = cleaned_reference

        if resolver == "telegram" and reference.get("source_locator"):
            val = reference.pop("source_locator")
            if val:
                reference.setdefault("tg_post_url", val)

        if not source_url and not reference:
            logger.info("Pomijam media bez rozpoznawalnego identyfikatora ani URL: %s", entry)
            continue

        if (
            resolver == "telegram"
            and source_url
            and reference.get("tg_post_url")
            and source_url == reference["tg_post_url"]
        ):
            logger.debug(
                "Usuwam adres źródłowy %s – wymagane pobranie przez resolver %s",
                source_url,
                resolver,
            )
            source_url = ""

        media_item: dict[str, Any] = {
            "type": media_type,
            "has_spoiler": has_spoiler,
        }
        if caption:
            media_item["caption"] = caption
        if source_label:
            media_item["source"] = source_label
        if posted_at:
            media_item["posted_at"] = posted_at
        if source_url:
            media_item["source_url"] = source_url
        if resolver:
            media_item["resolver"] = resolver
        if reference:
            media_item["reference"] = reference

        logger.debug(
            "Normalised media entry: type=%s resolver=%s source_url=%s reference=%s",
            media_type,
            resolver,
            source_url,
            reference,
        )

        normalised.append(media_item)

    return normalised[:5]


def _media_source_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    snapshot["type"] = str(item.get("type") or "").strip().lower()
    snapshot["resolver"] = str(item.get("resolver") or item.get("source") or "").strip().lower()
    snapshot["caption"] = str(item.get("caption") or "").strip()
    snapshot["posted_at"] = str(item.get("posted_at") or "").strip()
    snapshot_source = str(item.get("source_url") or item.get("url") or "").strip()
    snapshot["source"] = snapshot_source
    reference_raw = item.get("reference")
    if isinstance(reference_raw, dict):
        snapshot["reference"] = {k: reference_raw[k] for k in reference_raw if reference_raw[k] not in (None, "")}
    else:
        snapshot["reference"] = {}
    snapshot["status"] = "pending"
    return snapshot


def _guess_extension(media_type: str, content_type: str | None = None) -> str:
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    if media_type == "photo":
        return ".jpg"
    if media_type == "video":
        return ".mp4"
    if media_type == "doc":
        return ".bin"
    return ".bin"


def _persist_resolved_media(
    *,
    content: bytes,
    media_type: str,
    resolver: str,
    reference: dict[str, Any],
    content_type: str | None = None,
) -> str:
    if not content:
        return ""
    media_root = Path(settings.MEDIA_ROOT)
    cache_dir = media_root / "resolved"
    os.makedirs(cache_dir, exist_ok=True)
    ext = _guess_extension(media_type, content_type)
    fname = cache_dir / f"{uuid.uuid4().hex}{ext}"
    try:
        with open(fname, "wb") as fh:
            fh.write(content)
    except Exception:
        logger.exception(
            "Nie udało się zapisać pliku z resolvera %s (media=%s, ref=%s)",
            resolver,
            media_type,
            reference,
        )
        return ""
    return fname.as_posix()


def _resolve_media_reference(
    *,
    resolver: str,
    reference: dict[str, Any],
    media_type: str,
    caption: str = "",
) -> str:
    resolver = (resolver or "").strip().lower()
    if not resolver or not reference:
        return ""

    def _looks_like_asset(url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http"):
            return False
        path = parsed.path or ""
        if not path:
            return False
        ext = os.path.splitext(path)[-1].lower()
        return ext in {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".webm", ".pdf", ".mkv", ".avi"}

    def _reference_fallback_url() -> str:
        for key in (
            "direct_url",
            "download_url",
            "source_url",
            "tg_post_url",
            "source_locator",
            "tweet_url",
            "permalink",
            "url",
        ):
            value = reference.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    fallback_url = _reference_fallback_url()

    builtin_url = _resolve_with_builtin_resolver(resolver, reference, media_type, caption)
    if builtin_url:
        logger.info(
            "Resolved media via built-in resolver %s (ref=%s, url=%s)",
            resolver,
            reference,
            builtin_url,
        )
        return builtin_url

    base_url = os.getenv("MEDIA_RESOLVER_URL", "").strip()
    if not base_url:
        if fallback_url and _looks_like_asset(fallback_url):
            logger.info(
                "Brak MEDIA_RESOLVER_URL – używam bezpośredniego adresu %s (resolver=%s, ref=%s)",
                fallback_url,
                resolver,
                reference,
            )
            return fallback_url
        if fallback_url:
            logger.warning(
                "Pominięto fallback URL %s – wygląda na stronę HTML. Skonfiguruj TELEGRAM_RESOLVER_* lub MEDIA_RESOLVER_URL",
                fallback_url,
            )
        logger.warning(
            "Brak MEDIA_RESOLVER_URL – nie mogę rozwiązać identyfikatora %s (%s)",
            resolver,
            reference,
        )
        return ""

    endpoint = f"{base_url.rstrip('/')}/resolve/{resolver}"
    payload = {"media_type": media_type, "caption": caption or "", **reference}
    timeout_s = float(os.getenv("MEDIA_RESOLVER_TIMEOUT", 30))

    try:
        response = httpx.post(endpoint, json=payload, timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Resolver %s zwrócił HTTP %s dla %s", resolver, exc.response.status_code, reference
        )
        return ""
    except httpx.RequestError as exc:
        logger.warning("Błąd sieci przy resolverze %s: %s", resolver, exc)
        return ""

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = response.json()
        except ValueError:
            logger.warning("Resolver %s zwrócił niepoprawny JSON", resolver)
            return ""
        download_url = str(
            data.get("download_url")
            or data.get("url")
            or data.get("source_url")
            or ""
        ).strip()
        if download_url:
            logger.info(
                "Resolver %s zwrócił bezpośredni URL %s (ref=%s)",
                resolver,
                download_url,
                reference,
            )
            return download_url
        content_b64 = data.get("content_base64")
        if content_b64:
            try:
                binary = base64.b64decode(content_b64)
            except Exception:
                logger.exception("Nie udało się zdekodować treści base64 z resolvera %s", resolver)
                return ""
            persisted = _persist_resolved_media(
                content=binary,
                media_type=media_type,
                resolver=resolver,
                reference=reference,
                content_type=data.get("content_type"),
            )
            if persisted:
                logger.info(
                    "Resolver %s zwrócił treść base64 – zapisano %s (ref=%s)",
                    resolver,
                    persisted,
                    reference,
                )
            return persisted
        logger.warning("Resolver %s nie zwrócił żadnego URL ani danych", resolver)
        return ""

    binary = response.content
    if not binary:
        logger.warning("Resolver %s zwrócił pustą odpowiedź binarną", resolver)
        return ""
    persisted = _persist_resolved_media(
        content=binary,
        media_type=media_type,
        resolver=resolver,
        reference=reference,
        content_type=content_type,
    )
    if persisted:
        logger.info(
            "Resolver %s zwrócił dane binarne – zapisano %s (ref=%s)",
            resolver,
            persisted,
            reference,
        )
    return persisted


def _first_url_from(value: Any) -> str:
    url_keys = ("source_url", "download_url", "url", "image_url", "href")
    nested_keys = ("source", "asset", "file", "image", "media", "items", "data", "results", "variants")

    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            candidate = current.strip()
            if candidate:
                return candidate
            continue
        if isinstance(current, dict):
            for key in url_keys:
                raw_url = current.get(key)
                if isinstance(raw_url, str) and raw_url.strip():
                    return raw_url.strip()
            for key in nested_keys:
                nested = current.get(key)
                if nested:
                    stack.append(nested)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return ""


def _normalise_type(value: Any) -> str:
    mapped = str(value or "").strip().lower()
    aliases = {
        "image": "photo",
        "picture": "photo",
        "photo": "photo",
        "animation": "doc",
        "gif": "doc",
        "document": "doc",
        "file": "doc",
        "pdf": "doc",
    }
    if mapped in aliases:
        mapped = aliases[mapped]
    if mapped in {"photo", "video", "doc"}:
        return mapped
    return "photo"


def _resolve_with_builtin_resolver(
    resolver: str,
    reference: dict[str, Any],
    media_type: str,
    caption: str,
) -> str:
    resolver = (resolver or "").strip().lower()
    if resolver == "telegram":
        return _resolve_media_via_telegram(reference, media_type, caption)
    return ""


def _resolve_media_via_telegram(
    reference: dict[str, Any],
    media_type: str,
    caption: str,
) -> str:
    from apps.posts.resolvers import telegram as telegram_resolver

    tg_url = reference.get("tg_post_url") or reference.get("source_locator")
    if not tg_url:
        return ""
    logger.info("Wbudowany resolver Telegram – pobieram %s", tg_url)
    try:
        result = telegram_resolver.download_telegram_media(
            tg_url,
            media_type=media_type,
            caption=caption,
        )
    except telegram_resolver.TelegramMediaNotFound as exc:
        logger.warning("Wbudowany resolver Telegram nie znalazł mediów (%s): %s", tg_url, exc)
        return ""
    except telegram_resolver.TelegramResolverNotConfigured:
        logger.warning("Resolver Telegram nie został skonfigurowany – pomijam wbudowane pobieranie")
        return ""
    except Exception:
        logger.exception("Błąd wbudowanego resolvera telegram dla %s", tg_url)
        return ""

    if result:
        logger.info("Wbudowany resolver Telegram zakończył się sukcesem: %s", result)
        return result

    logger.warning("Wbudowany resolver Telegram nie zwrócił pliku dla %s", tg_url)
    return ""


def _parse_gpt_payload(raw: str) -> dict[str, Any] | None:
    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("GPT zwrócił niepoprawny JSON: %s", raw)
        return None
    if not isinstance(data, dict):
        return None
    post_data = data.get("post")
    post_payload: dict[str, Any] = {}
    text = ""
    if isinstance(post_data, dict):
        post_payload = dict(post_data)
        text = str(post_payload.get("text", "") or "").strip()
    legacy_text = str(data.get("post_text", "") or "").strip()
    if not text and legacy_text:
        text = legacy_text
    if not text:
        return None
    post_payload["text"] = text
    media = _normalise_media_payload(data.get("media"), _default_image_prompt(text))
    payload: dict[str, Any] = {
        "post": post_payload,
        "media": media,
        "raw_response": cleaned,
    }
    return payload


def gpt_generate_text(system_prompt: str, user_prompt: str, *, response_format: dict[str, Any] | None = None) -> str | None:
    try:
        cli = _client()
    except RuntimeError as exc:
        logger.warning("Pomijam generowanie GPT: %s", exc)
        return None
    try:
        model = os.getenv("OPENAI_MODEL", "gpt-4.1")
        temperature = float(os.getenv("OPENAI_TEMPERATURE", 0.2))

        use_tools = response_format is None
        if use_tools:
            try:
                response = cli.responses.create(
                    model=model,
                    temperature=temperature,
                    seed=_openai_seed(),
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
        seed = _openai_seed()
        if seed is not None:
            chat_kwargs["seed"] = seed
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

    instructions = [
        "Zwróć dokładnie jeden obiekt JSON zawierający pola:",
        "- post: obiekt z polem text zawierającym gotową treść posta zgodną z zasadami kanału;",
        "- media: lista 0-5 obiektów opisujących multimedia do posta.",
        "Każdy obiekt media MUSI mieć pola: type (photo/video/doc), resolver (np. twitter/telegram/instagram/rss) oraz reference – obiekt z prawdziwymi identyfikatorami źródła (np. {\"tg_post_url\": \"https://t.me/...\", \"posted_at\": \"2024-06-09T10:32:00Z\"}).",
        "Pole identyfikator (jeśli użyte) ma zawierać rzeczywistą wartość identyfikatora, a nie nazwę pola ani placeholder.",
        "Jeśli brak klucza specyficznego dla platformy, użyj reference.source_locator z kanonicznym adresem URL do zasobu.",
        "Używaj wyłącznie angielskich nazw pól w formacie snake_case (ASCII, bez spacji i znaków diakrytycznych).",
        "Nie podawaj bezpośrednich linków do plików – zwróć wyłącznie identyfikatory potrzebne do pobrania media po naszej stronie.",
        "Jeśli media pochodzą z artykułu lub innego źródła, dołącz dostępne metadane (caption, posted_at, author).",
        "Pole has_spoiler (true/false) jest opcjonalne i dotyczy wyłącznie zdjęć wymagających ukrycia.",
    ]

    if channel.max_chars:
        instructions.append(
            "Długość odpowiedzi musi mieścić się w limicie znaków opisanym w systemowym promptcie."
        )

    article_context = _article_context(article)
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
    payload = _parse_gpt_payload(raw)
    if payload is None:
        logger.warning("GPT draft response (channel=%s) nie zawiera poprawnego JSON", channel.id)
        return None
    logger.info(
        "GPT draft response (channel=%s): %s",
        channel.id,
        json.dumps(payload, ensure_ascii=False),
    )
    return payload

def gpt_rewrite_text(channel: Channel, text: str, editor_prompt: str) -> str:
    sys = _channel_system_prompt(channel)
    usr = (
        "Przepisz poniższy tekst zgodnie z zasadami i wytycznymi edytora. "
        "Zachowaj charakter kanału, wymagania dotyczące długości, emoji oraz stopki opisane w systemowym promptcie."

    )
    rewritten = gpt_generate_text(sys, usr)
    return rewritten or text


def _media_expiry_deadline():
    return timezone.now() + timedelta(days=int(os.getenv("MEDIA_CACHE_TTL_DAYS", 7)))


def attach_media_from_payload(post: Post, media_payload: list[dict[str, Any]]):
    post.media.all().delete()
    source_entries: list[dict[str, Any]] = []
    for idx, item in enumerate(media_payload):
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("type", "photo") or "photo").strip().lower()
        if media_type == "image":
            media_type = "photo"
        if media_type not in {"photo", "video", "doc"}:
            snapshot = _media_source_snapshot(item)
            snapshot.update({"status": "skipped", "error": "unsupported_type"})
            source_entries.append(snapshot)
            continue

        snapshot = _media_source_snapshot(item)
        resolver_name = snapshot.get("resolver", "")
        reference_data = dict(snapshot.get("reference") or {})
        original_source = snapshot.get("source", "")
        caption = snapshot.get("caption", "")
        posted_at = snapshot.get("posted_at", "")
        source_url = str(item.get("source_url") or item.get("url") or "").strip()

        if source_url:
            reference_data.setdefault("original_url", source_url)
        elif original_source:
            reference_data.setdefault("original_url", original_source)

        source_entry = {
            "type": media_type,
            "resolver": resolver_name,
            "caption": caption,
            "posted_at": posted_at,
            "source": source_url or original_source,
            "reference": dict(reference_data),
            "status": "pending",
        }

        if not source_url:
            if resolver_name and reference_data:
                logger.info(
                    "Resolving media via %s for post %s (ref=%s)",
                    resolver_name or "unknown",
                    post.id,
                    reference_data,
                )
                resolve_input = dict(reference_data)
                source_url = _resolve_media_reference(
                    resolver=resolver_name,
                    reference=resolve_input,
                    media_type=media_type,
                    caption=caption,
                )
                if source_url:
                    logger.info(
                        "Resolved media for post %s via %s (url=%s)",
                        post.id,
                        resolver_name or "unknown",
                        source_url,
                    )
            if not source_url:
                logger.warning(
                    "Nie udało się pobrać medium typu %s dla posta %s (resolver=%s, reference=%s)",
                    media_type,
                    post.id,
                    resolver_name or "brak",
                    reference_data,
                )
                source_entry["status"] = "unresolved"
                source_entry["error"] = "missing_source"
                source_entries.append(source_entry)
                continue

        reference_data.setdefault("resolved_url", source_url)
        if posted_at and "posted_at" not in reference_data:
            reference_data["posted_at"] = posted_at
        if caption and "caption" not in reference_data:
            reference_data["caption"] = caption

        has_spoiler = item.get("has_spoiler")
        if has_spoiler is None and media_type == "photo":
            has_spoiler = bool(getattr(post.channel, "auto_blur_default", False))
        else:
            has_spoiler = bool(has_spoiler)

        pm = PostMedia.objects.create(
            post=post,
            type=media_type,
            source_url=source_url,
            resolver=resolver_name,
            reference_data=reference_data,
            order=idx,
            has_spoiler=has_spoiler,
        )
        try:
            cache_path = cache_media(pm)
        except Exception:
            logger.exception("Nie udało się pobrać medium %s dla posta %s", pm.id, post.id)
            pm.delete()
            source_entry["status"] = "error"
            source_entry["error"] = "cache_failure"
            source_entries.append(source_entry)
            continue
        if not cache_path:
            logger.info(
                "Pomijam medium %s dla posta %s – brak cache po pobraniu (%s)",
                pm.id,
                post.id,
                source_url,
            )
            pm.delete()
            source_entry["status"] = "skipped"
            source_entry["error"] = "empty_cache"
        else:
            logger.info(
                "Media download completed for post %s (media_id=%s, path=%s)",
                post.id,
                pm.id,
                cache_path,
            )
            reference_data.setdefault("cache_path", cache_path)
            source_entry["status"] = "cached"
            source_entry["reference"] = reference_data
            source_entry["source"] = source_url
        source_entries.append(source_entry)

    post.source_metadata = {"media": source_entries}
    post.save(update_fields=["source_metadata"])


def create_post_from_payload(channel: Channel, payload: dict[str, Any]) -> Post:
    post_data = payload.get("post") or {}
    text = str(post_data.get("text") or payload.get("post_text", "") or "").strip()
    if not text:
        raise ValueError("Brak treści posta w odpowiedzi GPT")
    raw_payload = payload.get("raw_response")
    if raw_payload is None:
        raw_payload = json.dumps(payload, ensure_ascii=False)
    media_items = payload.get("media") or []
    source_meta_entries: list[dict[str, Any]] = []
    if isinstance(media_items, list):
        for raw_item in media_items:
            if isinstance(raw_item, dict):
                source_meta_entries.append(_media_source_snapshot(raw_item))
    post = Post.objects.create(
        channel=channel,
        text=text,
        status="DRAFT",
        origin="gpt",
        generated_prompt=raw_payload,
        source_metadata={"media": source_meta_entries} if source_meta_entries else {},
    )
    if isinstance(media_items, list):
        attach_media_from_payload(post, media_items)
    return post


def compute_dupe(post: Post) -> float:
    texts = Post.objects.filter(status="PUBLISHED").order_by("-id").values_list("text", flat=True)[:300]
    if not texts:
        return 0.0
    return max(fuzz.token_set_ratio(post.text, t) / 100.0 for t in texts)

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
        start += timezone.timedelta(days=1)
        end += timezone.timedelta(days=1)
        candidate = start

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
        if candidate > end or safety_counter > (24 * 60 // step) + 1:
            start += timezone.timedelta(days=1)
            end += timezone.timedelta(days=1)
            candidate = start
            safety_counter = 0
    return candidate

def assign_auto_slot(post: Post):
    if post.schedule_mode == "MANUAL":
        return
    post.scheduled_at = next_auto_slot(post.channel)
    post.dupe_score = compute_dupe(post)
    post.status = Post.Status.SCHEDULED
    post.save()


def approve_post(post: Post, user=None):
    """Mark a draft as approved and assign the next automatic publication slot."""

    post.status = Post.Status.APPROVED
    post.schedule_mode = "AUTO"
    if user and getattr(user, "is_authenticated", False):
        post.approved_by = user
    post.scheduled_at = next_auto_slot(post.channel)
    post.dupe_score = compute_dupe(post)
    if post.expires_at:
        post.expires_at = None
    post.save()
    return post

def purge_cache():
    for pm in PostMedia.objects.filter(expires_at__lt=timezone.now()):
        try:
            if pm.cache_path and os.path.exists(pm.cache_path):
                os.remove(pm.cache_path)
        finally:
            pm.cache_path = ""
            pm.save()

def cache_media(pm: PostMedia):
    if pm.cache_path and os.path.exists(pm.cache_path):
        return pm.cache_path
    url = (pm.source_url or "").strip()
    if not url:
        return ""
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
        if parsed.scheme == "file":
            src = unquote(path)
        else:
            src = url
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
