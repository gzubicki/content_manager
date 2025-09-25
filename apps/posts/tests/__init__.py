import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "content_manager.settings")

try:
    import django
    django.setup()
except Exception:  # pragma: no cover
    # Allow pytest to continue if Django is already set up elsewhere.
    pass
