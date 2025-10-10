import os
from pathlib import Path

import dj_database_url
from django.utils.translation import gettext_lazy as _
from dotenv import load_dotenv
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev")
DEBUG = bool(int(os.getenv("DEBUG", 1)))
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
LANGUAGE_CODE = os.getenv("LANGUAGE_CODE", "pl")
TIME_ZONE = os.getenv("TIME_ZONE", "Europe/Warsaw")
USE_I18N = True
USE_L10N = True
USE_TZ = True
LANGUAGES = [
    ("pl", _("Polski")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_ENGINE = os.getenv("SESSION_ENGINE", "django.contrib.sessions.backends.db")

# produkcja:
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True


INSTALLED_APPS = [
    "jazzmin",
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "apps.posts", "apps.accounts",
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "content_manager.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "content_manager.wsgi.application"
ASGI_APPLICATION = "content_manager.asgi.application"

STATIC_URL = os.getenv("STATIC_URL", "/static/")
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "content_manager.staticfiles.LenientCompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", BASE_DIR / "media"))
DATABASES = {"default": dj_database_url.parse(os.getenv("DATABASE_URL", "sqlite:///db.sqlite3"))}

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL



PWA_APP_NAME = os.getenv("PWA_APP_NAME", "Content Manager")
PWA_THEME_COLOR = os.getenv("PWA_THEME_COLOR", "#111827")
PWA_BACKGROUND_COLOR = os.getenv("PWA_BACKGROUND_COLOR", "#111827")
PWA_START_URL = os.getenv("PWA_START_URL", "/admin/")

MAX_POST_CHARS = int(os.getenv("MAX_POST_CHARS", 1000))
EMOJI_MIN = int(os.getenv("EMOJI_MIN", 1))
EMOJI_MAX = int(os.getenv("EMOJI_MAX", 6))
NO_LINKS_IN_TEXT = bool(int(os.getenv("NO_LINKS_IN_TEXT", 1)))

DRAFT_TARGET_COUNT = int(os.getenv("DRAFT_TARGET_COUNT", 20))
DRAFT_TTL_DAYS = int(os.getenv("DRAFT_TTL_DAYS", 3))
MEDIA_CACHE_TTL_DAYS = int(os.getenv("MEDIA_CACHE_TTL_DAYS", 7))
PUBLISHED_POST_TTL_DAYS = int(os.getenv("PUBLISHED_POST_TTL_DAYS", 30))
DEDUPE_THRESHOLD = float(os.getenv("DEDUPE_THRESHOLD", 0.85))
DEDUPE_WINDOW = int(os.getenv("DEDUPE_WINDOW", 300))

SLOT_STEP_MIN = int(os.getenv("SLOT_STEP_MIN", 30))
SLOT_START_HOUR = int(os.getenv("SLOT_START_HOUR", 6))
SLOT_END_HOUR = int(os.getenv("SLOT_END_HOUR", 23))
SLOT_END_MINUTE = int(os.getenv("SLOT_END_MINUTE", 30))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "")
TELEGRAM_LOGIN_ENABLED = bool(int(os.getenv("TELEGRAM_LOGIN_ENABLED", 1)))
TELEGRAM_LOGIN_BOT = os.getenv("TELEGRAM_LOGIN_BOT", "")
TELEGRAM_LOGIN_DOMAIN = os.getenv("TELEGRAM_LOGIN_DOMAIN", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", 0.3))



CELERY_BEAT_SCHEDULE = {
    "ensure_drafts": {
        "task": "apps.posts.tasks.task_ensure_min_drafts",
        "schedule": 60.0,  # co minutę
    },
    "publish_due": {
        "task": "apps.posts.tasks.task_publish_due",
        "schedule": 60.0,    # co minutę
    },
    "housekeeping": {
        "task": "apps.posts.tasks.task_housekeeping",
        "schedule": 3600.0,  # co godzinę
    },
}
