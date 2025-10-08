from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from apps.posts.resolvers import telegram as telegram_resolver

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency handled at runtime
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        PasswordHashInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberBannedError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.sessions import StringSession
except ImportError:  # pragma: no cover
    class _TelethonMissingError(Exception):
        """Fallback exception when Telethon is unavailable."""

    TelegramClient = None  # type: ignore[assignment]
    FloodWaitError = PasswordHashInvalidError = PhoneCodeExpiredError = PhoneCodeInvalidError = PhoneNumberBannedError = PhoneNumberInvalidError = SessionPasswordNeededError = _TelethonMissingError  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]


class TelegramResolverPhoneForm(forms.Form):
    phone = forms.CharField(
        label=_("Phone number"),
        max_length=32,
        help_text=_("Use international format, e.g. +48123456789."),
    )
    force_sms = forms.BooleanField(
        label=_("Force SMS"),
        required=False,
        help_text=_("Request the code via SMS instead of Telegram app."),
    )


class TelegramResolverCodeForm(forms.Form):
    code = forms.CharField(
        label=_("Login code"),
        max_length=16,
        help_text=_("Enter the 5-digit code from Telegram."),
    )


class TelegramResolverPasswordForm(forms.Form):
    password = forms.CharField(
        label=_("Two-factor password"),
        widget=forms.PasswordInput(render_value=False),
        help_text=_("If your Telegram account uses an additional password, enter it here."),
    )


SESSION_STEP_KEY = "telegram_resolver_step"
SESSION_PHONE_KEY = "telegram_resolver_phone"
SESSION_HASH_KEY = "telegram_resolver_phone_code_hash"
SESSION_OVERRIDE_KEY = "telegram_resolver_session_override"
SESSION_LAST_STRING_KEY = "telegram_resolver_last_session_string"


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    session_mode: str  # "path" or "string"
    session_path: str
    session_string: str
    session_dir: str
    session_name: str


def _load_resolver_config() -> tuple[Optional[TelegramConfig], Optional[str]]:
    if TelegramClient is None:
        return None, _("Python package 'telethon' is not installed. Run `pip install telethon`." )

    api_id_raw = os.getenv("TELEGRAM_RESOLVER_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_RESOLVER_API_HASH", "").strip()
    session_string = os.getenv("TELEGRAM_RESOLVER_SESSION", "").strip()
    session_path_raw = os.getenv("TELEGRAM_RESOLVER_SESSION_PATH", "").strip()
    session_dir_raw = os.getenv("TELEGRAM_RESOLVER_SESSION_DIR", str(settings.BASE_DIR / "var"))
    session_name = os.getenv("TELEGRAM_RESOLVER_SESSION_NAME", "tg_resolver")

    if not api_id_raw or not api_hash:
        return None, _("Set TELEGRAM_RESOLVER_API_ID and TELEGRAM_RESOLVER_API_HASH in the environment.")

    try:
        api_id = int(api_id_raw)
    except ValueError:
        return None, _("TELEGRAM_RESOLVER_API_ID must be an integer.")

    session_dir = Path(session_dir_raw)
    session_dir.mkdir(parents=True, exist_ok=True)

    if session_string:
        session_mode = "string"
        session_path = str(Path(session_path_raw).resolve()) if session_path_raw else ""
    else:
        session_mode = "path"
        session_file = Path(session_path_raw) if session_path_raw else session_dir / session_name
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_path = session_file.resolve().as_posix()

    config = TelegramConfig(
        api_id=api_id,
        api_hash=api_hash,
        session_mode=session_mode,
        session_path=session_path,
        session_string=session_string,
        session_dir=session_dir.as_posix(),
        session_name=session_name,
    )
    return config, None


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:  # pragma: no cover - fallback for nested loops
        if "asyncio.run() cannot" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        raise


def _create_client(config: TelegramConfig, session_override: Optional[str] = None) -> TelegramClient:
    if TelegramClient is None:  # pragma: no cover - guarded earlier
        raise telegram_resolver.TelegramResolverNotConfigured("Telethon not available")

    if config.session_mode == "string":
        if StringSession is None:  # pragma: no cover - guarded earlier
            raise telegram_resolver.TelegramResolverNotConfigured("Telethon StringSession missing")
        session_data = session_override or config.session_string or ""
        return TelegramClient(StringSession(session_data), config.api_id, config.api_hash)

    session_file = Path(config.session_path)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(session_file.as_posix(), config.api_id, config.api_hash)


def _send_code_request(
    config: TelegramConfig,
    phone: str,
    force_sms: bool,
    session_override: Optional[str],
) -> tuple[str, Optional[str]]:
    async def runner():
        client = _create_client(config, session_override)
        await client.connect()
        try:
            sent = await client.send_code_request(phone, force_sms=force_sms)
            saved_session = client.session.save()
            return sent.phone_code_hash, saved_session
        finally:
            await client.disconnect()

    return _run_async(runner())


def _sign_in_with_code(
    config: TelegramConfig,
    phone: str,
    code: str,
    phone_code_hash: str,
    session_override: Optional[str],
) -> tuple[str, Optional[str]]:
    async def runner():
        client = _create_client(config, session_override)
        await client.connect()
        try:
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                status = "ok"
            except SessionPasswordNeededError:
                status = "password"
            saved_session = client.session.save()
            return status, saved_session
        finally:
            await client.disconnect()

    return _run_async(runner())


def _sign_in_with_password(
    config: TelegramConfig,
    password: str,
    session_override: Optional[str],
) -> Optional[str]:
    async def runner():
        client = _create_client(config, session_override)
        await client.connect()
        try:
            await client.sign_in(password=password)
            return client.session.save()
        finally:
            await client.disconnect()

    return _run_async(runner())


def _check_authorization(
    config: TelegramConfig,
    session_override: Optional[str],
) -> tuple[bool, Optional[Any]]:
    async def runner():
        client = _create_client(config, session_override)
        await client.connect()
        try:
            authorized = await client.is_user_authorized()
            me = await client.get_me() if authorized else None
            return authorized, me
        finally:
            await client.disconnect()

    try:
        return _run_async(runner())
    except telegram_resolver.TelegramResolverNotConfigured:
        return False, None
    except Exception:  # pragma: no cover - status check only
        logger.exception("Nie udało się sprawdzić statusu autoryzacji Telegrama")
        return False, None


def _clear_session_state(request):
    for key in [SESSION_STEP_KEY, SESSION_PHONE_KEY, SESSION_HASH_KEY, SESSION_OVERRIDE_KEY]:
        request.session.pop(key, None)


@staff_member_required
def telegram_resolver_login(request):
    config, config_error = _load_resolver_config()
    session_override = request.session.get(SESSION_OVERRIDE_KEY)

    if request.GET.get("reset"):
        _clear_session_state(request)
        messages.info(request, _("Resolver workflow has been reset."))
        return redirect("telegram_resolver_login")

    state = request.session.get(SESSION_STEP_KEY, "phone")

    phone_form = TelegramResolverPhoneForm()
    code_form = TelegramResolverCodeForm()
    password_form = TelegramResolverPasswordForm()

    if request.method == "POST":
        step = request.POST.get("step") or state
        if not config:
            messages.error(request, config_error or _("Resolver configuration is missing."))
            return redirect("telegram_resolver_login")

        if step == "phone":
            phone_form = TelegramResolverPhoneForm(request.POST)
            if phone_form.is_valid():
                phone = phone_form.cleaned_data["phone"].strip()
                force_sms = phone_form.cleaned_data["force_sms"]
                try:
                    phone_code_hash, new_session = _send_code_request(config, phone, force_sms, session_override)
                except PhoneNumberInvalidError:
                    messages.error(request, _("Telegram reports the phone number is invalid."))
                except PhoneNumberBannedError:
                    messages.error(request, _("Telegram rejected this phone number (possibly banned)."))
                except FloodWaitError as exc:
                    messages.error(request, _( "Too many attempts. Wait %(seconds)d seconds before retrying." ) % {"seconds": exc.seconds})
                except telegram_resolver.TelegramResolverNotConfigured as exc:
                    messages.error(request, str(exc))
                except Exception:
                    logger.exception("Nie udało się wysłać kodu Telegram")
                    messages.error(request, _("Unexpected error while sending the Telegram code."))
                else:
                    request.session[SESSION_PHONE_KEY] = phone
                    request.session[SESSION_HASH_KEY] = phone_code_hash
                    request.session[SESSION_STEP_KEY] = "code"
                    if new_session and config.session_mode == "string":
                        request.session[SESSION_OVERRIDE_KEY] = new_session
                    messages.success(request, _("Verification code sent to %(phone)s." ) % {"phone": phone})
                    return redirect("telegram_resolver_login")
            state = "phone"

        elif step == "code":
            code_form = TelegramResolverCodeForm(request.POST)
            phone = request.session.get(SESSION_PHONE_KEY)
            phone_code_hash = request.session.get(SESSION_HASH_KEY)
            if not phone or not phone_code_hash:
                messages.error(request, _("Start with the phone number step."))
                return redirect("telegram_resolver_login")
            if code_form.is_valid():
                code = code_form.cleaned_data["code"].strip()
                try:
                    status, new_session = _sign_in_with_code(config, phone, code, phone_code_hash, session_override)
                except PhoneCodeInvalidError:
                    messages.error(request, _("Invalid verification code."))
                except PhoneCodeExpiredError:
                    messages.error(request, _("The verification code has expired. Request a new one."))
                    request.session.pop(SESSION_STEP_KEY, None)
                except FloodWaitError as exc:
                    messages.error(request, _( "Too many attempts. Wait %(seconds)d seconds before retrying." ) % {"seconds": exc.seconds})
                except telegram_resolver.TelegramResolverNotConfigured as exc:
                    messages.error(request, str(exc))
                except Exception:
                    logger.exception("Nie udało się uwierzytelnić kodem Telegram")
                    messages.error(request, _("Unexpected error while verifying the code."))
                else:
                    if new_session and config.session_mode == "string":
                        request.session[SESSION_OVERRIDE_KEY] = new_session
                    if status == "password":
                        request.session[SESSION_STEP_KEY] = "password"
                        messages.info(request, _("Enter the two-factor password to finish login."))
                    else:
                        if new_session and config.session_mode == "string":
                            request.session[SESSION_LAST_STRING_KEY] = new_session
                        _clear_session_state(request)
                        messages.success(request, _( "Telegram resolver authorized successfully." ))
                    return redirect("telegram_resolver_login")
            state = "code"

        elif step == "password":
            password_form = TelegramResolverPasswordForm(request.POST)
            if password_form.is_valid():
                password = password_form.cleaned_data["password"]
                try:
                    new_session = _sign_in_with_password(config, password, session_override)
                except PasswordHashInvalidError:
                    messages.error(request, _("Invalid two-factor password."))
                except telegram_resolver.TelegramResolverNotConfigured as exc:
                    messages.error(request, str(exc))
                except Exception:
                    logger.exception("Nie udało się uwierzytelnić hasłem Telegram")
                    messages.error(request, _("Unexpected error while verifying the password."))
                else:
                    if new_session and config.session_mode == "string":
                        request.session[SESSION_LAST_STRING_KEY] = new_session
                    _clear_session_state(request)
                    messages.success(request, _( "Telegram resolver authorized successfully." ))
                    return redirect("telegram_resolver_login")
            state = "password"

    # context preparation
    authorized = False
    authorized_user = None
    authorized_display = None
    config_ready = config is not None and config_error is None
    if config_ready:
        authorized, authorized_user = _check_authorization(config, session_override)
        if authorized and authorized_user is not None:
            name_bits = [getattr(authorized_user, "first_name", "") or "", getattr(authorized_user, "last_name", "") or ""]
            display = " ".join(bit for bit in name_bits if bit).strip()
            username = getattr(authorized_user, "username", None)
            if username:
                if display:
                    display = f"{display} (@{username})"
                else:
                    display = f"@{username}"
            if not display:
                display = str(getattr(authorized_user, "id", ""))
            authorized_display = display

    last_session_string = request.session.pop(SESSION_LAST_STRING_KEY, None)

    context = {
        "title": _("Telegram resolver authorization"),
        "config": config,
        "config_error": config_error,
        "config_ready": config_ready,
        "state": state if config_ready else "config",
        "phone_form": phone_form,
        "code_form": code_form,
        "password_form": password_form,
        "authorized": authorized,
        "authorized_user": authorized_user,
        "authorized_display": authorized_display,
        "session_override": session_override,
        "last_session_string": last_session_string,
    }

    return render(request, "posts/telegram_resolver_login.html", context)
