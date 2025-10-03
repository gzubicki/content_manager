#!/bin/sh
set -e

python manage.py makemigrations
python manage.py migrate --noinput
python manage.py collectstatic --noinput || true

if [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
email = os.environ["DJANGO_SUPERUSER_EMAIL"]
username = os.environ.get("DJANGO_SUPERUSER_USERNAME", email)
password = os.environ["DJANGO_SUPERUSER_PASSWORD"]
first_name = os.environ.get("DJANGO_SUPERUSER_FIRST_NAME", "")
last_name = os.environ.get("DJANGO_SUPERUSER_LAST_NAME", "")

user, created = User.objects.get_or_create(
    username=username,
    defaults={
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "is_staff": True,
        "is_superuser": True,
    },
)
if created:
    user.set_password(password)
    user.save()
else:
    updated = False
    if user.email != email:
        user.email = email
        updated = True
    if first_name and user.first_name != first_name:
        user.first_name = first_name
        updated = True
    if last_name and user.last_name != last_name:
        user.last_name = last_name
        updated = True
    if not user.is_superuser or not user.is_staff:
        user.is_superuser = True
        user.is_staff = True
        updated = True
    if updated:
        user.save()
    if not user.check_password(password):
        user.set_password(password)
        user.save()
PY
fi

exec "$@"
