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

wait_for_db() {
  local max_attempts=${1:-30}
  local sleep_seconds=${2:-2}
  local attempt=1

  echo "⏳ Czekam na bazę danych (maks ${max_attempts} prób)…"
  while [ "$attempt" -le "$max_attempts" ]; do
    if docker compose -f docker-compose.prod.yml exec -T db \
      sh -c 'pg_isready -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}"' \
        >/dev/null 2>&1; then
      echo "✅ Baza danych gotowa (próba ${attempt})."
      return 0
    fi

    attempt=$((attempt + 1))
    sleep "$sleep_seconds"
  done

  echo "❌ Baza danych nie wystartowała w oczekiwanym czasie." >&2
  exit 1
}

wait_for_db

docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker image prune -f
echo "✅ Deploy OK: $IMAGE"

