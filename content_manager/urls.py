from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, re_path
from django.views.generic import TemplateView

from apps.accounts.views import telegram_login, telegram_bind

urlpatterns = [
    path("admin/", admin.site.urls),
    path("auth/telegram/login/", telegram_login, name="tg_login"),
    path("auth/telegram/bind/", telegram_bind, name="tg_bind"),
    re_path(r"^sw\.js$", TemplateView.as_view(template_name="sw.js", content_type="application/javascript")),
    path("manifest.webmanifest", TemplateView.as_view(template_name="manifest.webmanifest", content_type="application/manifest+json")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
