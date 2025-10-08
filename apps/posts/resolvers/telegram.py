import asyncio
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from django.conf import settings

try:
    from telethon import TelegramClient
    from telethon.errors import RPCError
    from telethon.sessions import StringSession
except ImportError:  # pragma: no cover - optional dependency
    TelegramClient = None  # type: ignore[misc,assignment]


logger = logging.getLogger(__name__)


class TelegramResolverNotConfigured(RuntimeError):
    """Raised when Telegram resolver does not have required credentials."""


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
                logger.info("Brak mediów w wiadomości Telegram %s/%s", chat, message_id)
                return None
            dest_dir = Path(settings.MEDIA_ROOT) / "resolved" / "telegram"
            dest_dir.mkdir(parents=True, exist_ok=True)
            file_path = await client.download_media(message, file=dest_dir)
            if not file_path:
                return None
            return Path(file_path).resolve().as_uri()
        except TelegramResolverNotConfigured:
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
        return asyncio.run(_run()) or ""
    except TelegramResolverNotConfigured:
        raise
    except RuntimeError as exc:  # pragma: no cover - nested loop
        if "asyncio.run() cannot" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(_run()) or ""
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
