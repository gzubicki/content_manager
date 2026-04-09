
Drafty generowane wyłącznie przez GPT. Brak fallbacku na tekst statyczny.

## Minimalny bootstrap
Rzeczywista minimalna kolejność uruchomienia (zgodna z aktualnym kodem):

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Następnie:
1. Zaloguj się do panelu admina: `http://localhost:8000/admin/`.
2. Dodaj co najmniej jeden rekord **Kanał** ręcznie w panelu admina (`posts > Kanały`).
3. (Opcjonalnie) Dodaj źródła przez `posts > Źródła kanału`.

> Nie ma komendy `python manage.py init_channels` w tym repozytorium — inicjalizacja kanałów jest wykonywana ręcznie przez panel administracyjny.

- Celery worker/beat startują automatycznie przez `docker compose`.
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

## Komendy `manage.py` użyte w tym README vs kod

Zweryfikowano względem `apps/*/management/commands/`:

- ✅ `python manage.py migrate` — komenda wbudowana Django.
- ✅ `python manage.py createsuperuser` — komenda wbudowana Django.
- ❌ `python manage.py init_channels` — **usunięta z README**, brak implementacji w `apps/*/management/commands/`.

Aktualnie jedyna niestandardowa komenda z kodu aplikacji:

```bash
docker compose exec web python manage.py generate_draft_prompt <channel_id_lub_slug> [--no-headlines] [--article '{"title":"..."}'] [--avoid "tekst"]
```


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
