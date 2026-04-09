from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import login
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt

from .models import TelegramAccount
from .telegram_sso import verify_telegram_auth


def _build_technical_username(telegram_id: int) -> str:
    base_username = f"tg_{telegram_id}"
    username = base_username
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f"{base_username}_{suffix}"
        suffix += 1
    return username


@csrf_exempt
def telegram_login(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST only')
    data = request.POST.dict()
    if not verify_telegram_auth(data):
        return HttpResponseBadRequest('bad hash')
    tg_id_raw = data.get("id")
    if not tg_id_raw:
        return HttpResponseBadRequest("missing telegram id")
    try:
        tg_id = int(tg_id_raw)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid telegram id")

    auth_date = None
    auth_date_raw = data.get("auth_date")
    if auth_date_raw:
        try:
            auth_date = datetime.fromtimestamp(int(auth_date_raw), tz=dt_timezone.utc)
        except (TypeError, ValueError, OSError):
            auth_date = None

    with transaction.atomic():
        telegram_account = TelegramAccount.objects.select_related("user").filter(telegram_id=tg_id).first()
        if telegram_account is None:
            first_name = data.get("first_name", "")
            last_name = data.get("last_name", "")
            user = User.objects.create(
                username=_build_technical_username(tg_id),
                first_name=first_name,
                last_name=last_name,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
            telegram_account = TelegramAccount.objects.create(
                user=user,
                telegram_id=tg_id,
                telegram_username=data.get("username", ""),
                telegram_auth_date=auth_date,
            )
        else:
            changed = False
            tg_username = data.get("username", "")
            if telegram_account.telegram_username != tg_username:
                telegram_account.telegram_username = tg_username
                changed = True
            if auth_date is not None and telegram_account.telegram_auth_date != auth_date:
                telegram_account.telegram_auth_date = auth_date
                changed = True
            if changed:
                telegram_account.save(update_fields=["telegram_username", "telegram_auth_date"])
        user = telegram_account.user
    login(request, user)
    return HttpResponseRedirect('/admin/')

@csrf_exempt
def telegram_bind(request):
    return HttpResponseRedirect('/admin/')
