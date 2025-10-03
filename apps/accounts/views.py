from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from .telegram_sso import verify_telegram_auth

@csrf_exempt
def telegram_login(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST only')
    data = request.POST.dict()
    if not verify_telegram_auth(data):
        return HttpResponseBadRequest('bad hash')
    tg_id = data.get('id')
    username = data.get('username') or f"tg_{tg_id}"
    user, _ = User.objects.get_or_create(username=username)
    login(request, user)
    return HttpResponseRedirect('/admin/')

@csrf_exempt
def telegram_bind(request):
    return HttpResponseRedirect('/admin/')
