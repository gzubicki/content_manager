import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from django.conf import settings

try:
    from telethon import TelegramClient
    from telethon.errors import RPCError
    from telethon.sessions import StringSession
except ImportError:  # pragma: no cover - optional dependency
    TelegramClient = None  # type: ignore[misc,assignment]


logger = logging.getLogger(__name__)

_ALBUM_CACHE: Dict[Tuple[str, int], List[str]] = {}
_ALBUM_CACHE_LIMIT = 128


class TelegramResolverNotConfigured(RuntimeError):
    """Raised when Telegram resolver does not have required credentials."""


class TelegramMediaNotFound(RuntimeError):
    """Raised when Telegram message does not contain downloadable media."""


def download_telegram_media(
    tg_post_url: str,
    *,
    media_type: str,
    caption: str,
) -> str:
    """Download Telegram media and return local file URI.

    Requires TELEGRAM_RESOLVER_API_ID, TELEGRAM_RESOLVER_API_HASH and either
    TELEGRAM_RESOLVER_SESSION (string session) or TELEGRAM_RESOLVER_SESSION_PATH.
    """

    if TelegramClient is None:
        raise TelegramResolverNotConfigured("telethon not installed")

    api_id_raw = os.getenv("TELEGRAM_RESOLVER_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_RESOLVER_API_HASH", "").strip()
    session_string = os.getenv("TELEGRAM_RESOLVER_SESSION", "").strip()
    session_path = os.getenv("TELEGRAM_RESOLVER_SESSION_PATH", "").strip()

    if not api_id_raw or not api_hash:
        raise TelegramResolverNotConfigured("Missing TELEGRAM_RESOLVER_API_ID/API_HASH")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:  # pragma: no cover - config error
        raise TelegramResolverNotConfigured("TELEGRAM_RESOLVER_API_ID must be int") from exc

    chat, message_id = _parse_telegram_url(tg_post_url)
    if not chat or not message_id:
        logger.info("Niepoprawny tg_post_url: %s", tg_post_url)
        return ""

    async def _run() -> Optional[str]:
        client = _build_client(api_id, api_hash, session_string, session_path)
        if client is None:
            raise TelegramResolverNotConfigured("Unable to initialize Telegram client")
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramResolverNotConfigured(
                    "Telegram resolver session is not authorized; provide TELEGRAM_RESOLVER_SESSION"
                )
            entity = await client.get_entity(chat)
            message = await client.get_messages(entity, ids=message_id)
            if not message or not message.media:
                raise TelegramMediaNotFound(f"No media in Telegram message {chat}/{message_id}")
            dest_dir = Path(settings.MEDIA_ROOT) / "resolved" / "telegram"
            dest_dir.mkdir(parents=True, exist_ok=True)

            grouped_id = getattr(message, "grouped_id", None)
            cache_key = (chat, grouped_id or message_id)

            cached = _ALBUM_CACHE.get(cache_key)
            if cached:
                next_uri = cached.pop(0)
                if not cached:
                    _ALBUM_CACHE.pop(cache_key, None)
                return next_uri

            messages_to_download = await _collect_album_messages(client, entity, message)
            uris: List[str] = []
            for item in messages_to_download:
                if not getattr(item, "media", None):
                    continue
                file_path = await client.download_media(item, file=dest_dir)
                if not file_path:
                    continue
                uris.append(Path(file_path).resolve().as_uri())

            if not uris:
                raise TelegramMediaNotFound(f"Unable to download media for {chat}/{message_id}")

            if len(uris) > 1:
                if len(_ALBUM_CACHE) >= _ALBUM_CACHE_LIMIT:
                    _ALBUM_CACHE.pop(next(iter(_ALBUM_CACHE)))
                _ALBUM_CACHE[cache_key] = uris[1:]
            return uris[0]
        except TelegramResolverNotConfigured:
            raise
        except TelegramMediaNotFound:
            raise
        except RPCError as exc:  # pragma: no cover - network/api issues
            logger.warning("Telegram RPC error: %s", exc)
            return None
        except Exception:  # pragma: no cover - unexpected
            logger.exception("Telegram resolver unexpected error for %s", tg_post_url)
            return None
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        result = asyncio.run(_run())
        return result or ""
    except TelegramResolverNotConfigured:
        raise
    except TelegramMediaNotFound:
        raise
    except RuntimeError as exc:  # pragma: no cover - nested loop
        if "asyncio.run() cannot" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(_run())
                return result or ""
            finally:
                loop.close()
        raise


def _build_client(api_id: int, api_hash: str, session_string: str, session_path: str):
    if session_string:
        return TelegramClient(StringSession(session_string), api_id, api_hash)
    session_name = os.getenv("TELEGRAM_RESOLVER_SESSION_NAME", "tg_resolver")
    if not session_path:
        session_dir = Path(os.getenv("TELEGRAM_RESOLVER_SESSION_DIR", settings.BASE_DIR / "var"))
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / session_name
    else:
        session_file = Path(session_path)
        session_file.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(session_file.as_posix(), api_id, api_hash)


def _parse_telegram_url(url: str):
    parsed = urlparse(url)
    path = parsed.path or ""
    path = path.strip("/")
    if path.startswith("s/"):
        path = path[2:]
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) < 2:
        return None, None
    chat = parts[0]
    try:
        message_id = int(parts[1])
    except ValueError:
        # urls with message slug? ignore
        return None, None
    return chat, message_id


async def _collect_album_messages(client, entity, message):
    grouped_id = getattr(message, "grouped_id", None)
    if not grouped_id:
        return [message]

    ids = list(range(max(1, message.id - 16), message.id + 17))
    fetched = await client.get_messages(entity, ids=ids)
    album = [msg for msg in fetched if msg and getattr(msg, "media", None) and msg.grouped_id == grouped_id]
    if not album:
        return [message]
    album.sort(key=lambda m: m.id)
    unique: List[Any] = []
    seen = set()
    for msg in album:
        if msg.id in seen:
            continue
        seen.add(msg.id)
        unique.append(msg)
    return unique or [message]


async def _collect_album_messages(client, entity, message):
    grouped_id = getattr(message, "grouped_id", None)
    if not grouped_id:
        return [message]

    ids = list(range(max(1, message.id - 16), message.id + 17))
    fetched = await client.get_messages(entity, ids=ids)
    album = [msg for msg in fetched if msg and getattr(msg, "media", None) and msg.grouped_id == grouped_id]
    if not album:
        return [message]
    album.sort(key=lambda m: m.id)
    unique = []
    seen = set()
    for msg in album:
        if msg.id in seen:
            continue
        seen.add(msg.id)
        unique.append(msg)
    return unique
