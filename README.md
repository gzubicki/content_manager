
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



## ENV (wymagane)
- `DATABASE_URL=postgres://app:pass@db:5432/app`
- `REDIS_URL=redis://redis:6379/0`
- `OPENAI_API_KEY=...`
- `OPENAI_MODEL=gpt-4o`
- `TG_BOT_TOKEN=...` (+ bot adminem kanału)
