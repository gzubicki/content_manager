
Drafty generowane wyłącznie przez GPT. Brak fallbacku na tekst statyczny.

## Start
```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py init_channels
docker compose restart worker beat
```

- Admin: `http://localhost:8000/admin/`
- Celery worker/beat startują automatycznie.
- PWA: dodaj do ekranu w Chrome na Androidzie.



## Tworzenie superadmina

Aby dodać nowego superadministratora w środowisku dockerowym:

1. Upewnij się, że kontenery są uruchomione (`docker compose up -d`).
2. Uruchom polecenie:

   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

3. Podaj wymagane dane (adres e-mail, hasło itp.) w interaktywnym kreatorze.

Po zakończeniu logowanie do panelu administracyjnego będzie możliwe pod [http://localhost:8000/admin/](http://localhost:8000/admin/).


## ENV (wymagane)
- `DATABASE_URL=postgres://app:pass@db:5432/app`
- `REDIS_URL=redis://redis:6379/0`
- `OPENAI_API_KEY=...`
- `OPENAI_MODEL=gpt-5` (model musi obsługiwać narzędzie `web_search`)
- `TG_BOT_TOKEN=...` (+ bot adminem kanału)

## ENV (opcjonalne)
- `OPENAI_TIMEOUT` – niestandardowy limit czasu żądań do OpenAI (domyślnie 60 s).
- `OPENAI_MAX_RETRIES` – liczba ponowień na poziomie klienta OpenAI.
- `OPENAI_BASE_URL` – alternatywny endpoint (np. Azure/OpenAI-proxy).
- `OPENAI_ORG` – identyfikator organizacji OpenAI.
- `OPENAI_PROJECT` – identyfikator projektu OpenAI.
- `SESSION_COOKIE_SECURE` – flaga `Secure` dla ciasteczka sesji (`0/1`, `true/false`).
- `CSRF_COOKIE_SECURE` – flaga `Secure` dla ciasteczka CSRF (`0/1`, `true/false`).

### Rekomendowane wartości (dev/prod)
- **local/dev**: `ENV=dev`, `SESSION_COOKIE_SECURE=0`, `CSRF_COOKIE_SECURE=0`
- **produkcja**: `ENV=production`, `SESSION_COOKIE_SECURE=1`, `CSRF_COOKIE_SECURE=1`

Jeśli `SESSION_COOKIE_SECURE` i `CSRF_COOKIE_SECURE` nie są ustawione, aplikacja dobiera domyślne wartości na podstawie `ENV`:
- `ENV=dev` (lub brak `ENV`) → obie flagi `False`
- `ENV=prod` / `ENV=production` → obie flagi `True`

## Checklist uruchomienia (cookie security)
1. Skopiuj `.env.example` do `.env` i ustaw `ENV` odpowiednio do środowiska.
2. Dla dev zostaw `SESSION_COOKIE_SECURE=0` i `CSRF_COOKIE_SECURE=0`.
3. Dla produkcji ustaw `SESSION_COOKIE_SECURE=1` i `CSRF_COOKIE_SECURE=1`.
4. Uruchom aplikację i sprawdź wartości:
   ```bash
   docker compose exec web python manage.py shell -c "import os; from django.conf import settings; print('ENV=', os.getenv('ENV')); print('SESSION_COOKIE_SECURE=', settings.SESSION_COOKIE_SECURE); print('CSRF_COOKIE_SECURE=', settings.CSRF_COOKIE_SECURE)"
   ```
5. W przeglądarce (DevTools → Application/Cookies) potwierdź, że ciasteczka mają flagę `Secure` zgodnie z konfiguracją.
