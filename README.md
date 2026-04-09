Drafty generowane wyłącznie przez GPT. Brak fallbacku na tekst statyczny.

## Start
```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose restart worker beat
```

- Admin: `http://localhost:8000/admin/`
- Celery worker/beat startują automatycznie.
- Jeśli używasz własnego seeda kanałów, uruchom odpowiednią komendę inicjalizacyjną projektu.
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

## Architektura

System składa się z kilku współpracujących usług:

- **Django (web/admin)** – panel redakcyjny, modele i logika biznesowa.
- **Celery worker** – wykonuje zadania asynchroniczne (generowanie draftów, publikacja, housekeeping).
- **Celery beat** – cyklicznie uruchamia taski utrzymaniowe i publikacyjne.
- **Redis** – broker Celery + backend wyników + lock dla `ensure_min_drafts`.
- **Postgres** – trwałe dane aplikacji (`Channel`, `Post`, `PostMedia`, metadane).
- **Telegram Bot API** – docelowy kanał publikacji wpisów.
- **OpenAI Responses API** – generowanie draftów i rewrite (z wymuszonym narzędziem `web_search`).

### Szybka nawigacja po kodzie

- `apps/posts/tasks.py` – orkiestracja tasków Celery (generowanie, publikacja, housekeeping).
- `apps/posts/services.py` – logika GPT, publikacji, planowania slotów, cache mediów.
- `apps/posts/models.py` – modele domenowe (`Channel`, `Post`, `PostMedia`) i statusy.

## Model danych i statusy

Główny workflow opiera się o `Post.Status`:

- `DRAFT` – świeżo wygenerowany lub cofnięty do ręcznej poprawy.
- `APPROVED` – zatwierdzony przez redaktora.
- `SCHEDULED` – ma przypisany termin publikacji.
- `PUBLISHING` – trwa próba publikacji do Telegrama.
- `PUBLISHED` – opublikowany poprawnie.
- `REJECTED` – odrzucony redakcyjnie (status dostępny w modelu).

### Kiedy występują przejścia statusów

- **`DRAFT -> APPROVED/SCHEDULED`**: podczas akceptacji (`approve_post`) post dostaje auto-slot i finalnie jest zapisywany jako `SCHEDULED`.
- **`APPROVED -> SCHEDULED`**: w `Post.save()` gdy wpis ma ustawione `scheduled_at`.
- **`APPROVED/SCHEDULED -> PUBLISHING`**: `task_publish_due` bierze due posty i atomowo przełącza je na `PUBLISHING`.
- **`PUBLISHING -> PUBLISHED`**: `publish_post` po poprawnym wysłaniu wiadomości/media group do Telegrama.
- **`PUBLISHING -> APPROVED/SCHEDULED`**: przy błędach publikacji sieci/wyjątkach (rollback do statusu zależnego od obecności `scheduled_at`).
- **`PUBLISHING -> DRAFT`**: przy błędzie `entity too large` (wymagana ręczna korekta treści/media).
- **`DRAFT (expired)` -> usunięcie**: housekeeping usuwa przeterminowane drafty (`expires_at`).

## Proces generowania i publikacji

### 1) Utrzymanie minimalnej puli draftów

- Beat uruchamia `task_ensure_min_drafts` co **60s**.
- Task zakłada lock w Redis (`posts:ensure_min_drafts:lock`, TTL 10 minut), żeby uniknąć równoległej nadprodukcji.
- Dla kanałów z brakami kolejkuje `task_gpt_generate_for_channel(channel_id, need)`.

### 2) Generowanie treści GPT

- `task_gpt_generate_for_channel` wywołuje `services.gpt_new_draft` i `create_post_from_payload`.
- Task ma auto-retry dla `APIError`, `APIConnectionError`, `APITimeoutError`, `RateLimitError`:
  - `retry_backoff=True`
  - `retry_jitter=True`
  - `max_retries=6`
- Powiązane taski:
  - `task_gpt_generate_from_article` (retry jak wyżej, `max_retries=6`),
  - `task_gpt_generate_one` (rate limit `1/s`, `max_retries=5`),
  - `task_gpt_rewrite_post` (rewrite istniejącego draftu/posta).

### 3) Publikacja

- Beat uruchamia `task_publish_due` co **60s**.
- Task wybiera wpisy due (`APPROVED`/`SCHEDULED`, `scheduled_at <= now`), przełącza je na `PUBLISHING` i deleguje `publish_post`.
- `publish_post`:
  - zapisuje metadane „publication pending”,
  - publikuje media group + tekst lub sam tekst,
  - przy sukcesie zapisuje `message_id`, `published_at`, `dupe_score` i status `PUBLISHED`,
  - przy błędach aktualizuje metadane `publication failed` oraz cofa status (lub przenosi do `DRAFT`, jeśli payload za duży).

### 4) Housekeeping

- Beat uruchamia `task_housekeeping` co **3600s (1h)**.
- Task czyści dane wg TTL i reguł stale schedule (szczegóły niżej).

## Housekeeping/TTL

- **Draft TTL**: `Post.save()` ustawia `expires_at = now + draft_ttl_days` dla statusu `DRAFT`.
- **Draft cleanup**: `task_housekeeping` usuwa `DRAFT` z `expires_at < now`.
- **Published retention**: jeśli `PUBLISHED_POST_TTL_DAYS > 0`, housekeeping usuwa stare `PUBLISHED` (`published_at < cutoff`).
- **Stale schedule recovery**: jeśli `STALE_SCHEDULE_GRACE_MINUTES > 0`, wpisy `SCHEDULED/PUBLISHING` przeterminowane względem `scheduled_at` wracają do `DRAFT` i dostają znacznik błędu publikacji.
- **Media cache TTL**: `PostMedia.expires_at` liczony wg `MEDIA_CACHE_TTL_DAYS`; `purge_cache()` usuwa przeterminowane pliki z `MEDIA_ROOT/cache` i czyści `cache_path`.

## Wymagane i opcjonalne ENV

### Wymagane (produkcyjnie)

- `DATABASE_URL` (zalecany Postgres, np. `postgres://app:pass@db:5432/app`)
  - zła wartość/schemat -> `ImproperlyConfigured` podczas startu Django.
- `REDIS_URL` (np. `redis://redis:6379/0`)
  - brak/nieosiągalny Redis -> worker/beat nie kolejkowuje i nie wykonuje tasków.
- `OPENAI_API_KEY`
  - brak -> `RuntimeError` przy inicjalizacji klienta OpenAI, brak generacji draftów/rewrite.
- `OPENAI_MODEL`
  - model musi działać z Responses API i obsługiwać wymuszone narzędzie `web_search`; błędny model zwykle kończy się błędem żądania do API.
- `TG_BOT_TOKEN` (globalnie) + poprawny token bota przypisany do kanału (`Channel.bot_token`) i uprawnienia publikacji
  - bez tokena/uprawnień `publish_post` nie opublikuje wpisu (status zostanie cofnięty, błąd zapisany w metadanych).

### Opcjonalne

- `OPENAI_TIMEOUT` (domyślnie 60s)
  - wartość <= 0 lub nienumeryczna -> fallback do 60s + warning w logach.
- `OPENAI_MAX_RETRIES` (domyślnie 0)
  - wartość nienumeryczna/ujemna -> fallback do `0` + warning.
- `OPENAI_BASE_URL`
  - niepoprawny endpoint -> błędy połączenia/HTTP od dostawcy proxy.
- `OPENAI_ORG`, `OPENAI_PROJECT`
  - błędne ID zwykle skutkują odmową/autoryzacją po stronie API.
- `MEDIA_CACHE_TTL_DAYS` (domyślnie 7)
  - zbyt mało: częstsze redownloady mediów; zbyt dużo: większe zużycie dysku.
- `PUBLISHED_POST_TTL_DAYS` (domyślnie 30; `0` = brak kasowania historii)
  - zbyt mało: szybka utrata historii publikacji.
- `STALE_SCHEDULE_GRACE_MINUTES` (domyślnie 60; `0` = wyłączone)
  - zbyt mało: agresywne cofanie wpisów do draftów.
- `DRAFT_TARGET_COUNT`, `DRAFT_TTL_DAYS`
  - niskie wartości: ryzyko braku gotowych draftów; wysokie: większy koszt generacji i większa rotacja treści.
