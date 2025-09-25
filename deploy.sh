#!/usr/bin/env bash
set -euo pipefail

# wczytaj .env niezależnie od sesji
set -a
[ -f /opt/content_manager/.env ] && . /opt/content_manager/.env
set +a

: "${IMAGE:?Brak IMAGE w /opt/content_manager/.env}"

cd /opt/content_manager

# jeśli podane GHCR_USER/GHCR_PAT i brak logowania – zaloguj (dla prywatnych obrazów)
if [[ -n "${GHCR_USER:-}" && -n "${GHCR_PAT:-}" ]]; then
  if ! docker pull "$IMAGE" >/dev/null 2>&1; then
    echo "🔐 Logowanie do ghcr.io…"
    echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
  fi
fi

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker image prune -f
echo "✅ Deploy OK: $IMAGE"

