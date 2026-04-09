# Strategia pozyskiwania draftów bez zależności od web-search w OpenAI

## Kontekst problemu

Aktualny mechanizm opiera się na tym, że model OpenAI sam wyszukuje treści. W praktyce dla źródeł takich jak X/Twitter, Telegram i Facebook często daje to posty bez mediów (lub z niepełnym kontekstem), bo:

- platformy ograniczają boty i scrapowanie,
- część treści jest renderowana dynamicznie,
- metadane Open Graph bywają okrojone,
- API wyszukiwania nie ma gwarancji pełnej zgodności z oryginałem posta.

W nowym podejściu OpenAI powinno odpowiadać tylko za redakcję/normalizację treści, a nie za pobieranie danych źródłowych.

---

## Rekomendowany scenariusz (best option)

**Najlepszy kompromis stabilności, kosztu i utrzymania:**

1. **Warstwa pozyskiwania (collector) poza OpenAI**:
   - **Telegram**: oficjalne `Telegram Bot API` (własne kanały) lub `Telegram client API` (Telethon) dla publicznych kanałów bez bota.
   - **X**: preferencyjnie oficjalne API X (jeśli budżet pozwala); fallback przez automatyzację przeglądarki (Bytebot/Playwright) tylko dla dozwolonych kont.
   - **Facebook**: `Meta Graph API` (strony, do których masz uprawnienia), bez polegania na scrapingu profili prywatnych.
2. **Warstwa orkiestracji**: **n8n** jako workflow ETL (harmonogram, retry, alerty, dead-letter).
3. **Warstwa normalizacji**: zapis surowych danych i mediów do wspólnego formatu `SourceItem`.
4. **Warstwa redakcji AI**: OpenAI dostaje już pełny, znormalizowany payload (tekst + linki do mediów + metadata), i tylko:
   - streszcza,
   - redaguje styl,
   - proponuje hook/CTA/hashtagi,
   - opcjonalnie tłumaczy.

### Wariant preferowany: n8n-first (gotowe integracje)

Jeśli mamy już n8n, to **najpierw używamy natywnych node'ów/integracji n8n** dla Telegrama, X i Facebooka, a dopiero później dokładamy custom kod.

Proponowana kolejność implementacji:

1. **Telegram node/API w n8n** -> pobranie wiadomości + `file_id`/media + metadanych.
2. **X node lub HTTP Request do X API v2** -> pobranie posta + `includes.media`.
3. **Facebook Graph API przez node/HTTP Request w n8n** -> posty strony + `attachments`.
4. **Function node** -> normalizacja do wspólnego schematu `SourceItem`.
5. **HTTP Request node do Django** -> zapis draftu surowego i enqueue rewrite.

Dzięki temu minimalizujemy ilość kodu utrzymaniowego i szybciej uruchomimy wersję produkcyjną.

### Dlaczego ten scenariusz jest najlepszy

- Minimalizuje ryzyko utraty mediów (media są pobierane bezpośrednio ze źródła).
- Pozwala precyzyjnie kontrolować compliance (szczególnie dla X/Facebook).
- Nie uzależnia jakości draftów od bieżącej „widoczności” stron dla modelu.
- Ułatwia debug: można porównać `raw payload` vs `draft AI`.
- n8n skraca czas wdrożenia (mniej kodu „klejowego” w Django/Celery).

---

## Architektura docelowa

## 1) Ingestion adapters (per platforma)

Każdy adapter zapisuje rekord w tabeli/kolekcji `source_items_raw`:

- `source_platform` (`telegram|x|facebook`)
- `source_account_id`
- `source_post_id`
- `published_at`
- `text_raw`
- `media[]` (url, typ, mime, checksum, duration/wymiary)
- `engagement` (likes/reposts/views jeśli dostępne)
- `source_url`
- `ingested_at`
- `raw_json`

## 2) Media pipeline

- Pobranie plików do storage (S3/MinIO/local cache).
- Wyliczenie hashy (`sha256`) dla deduplikacji.
- Normalizacja formatów miniatur i metadanych.
- Oznaczanie błędów (np. geoblock, 403, expired URL).

## 3) Draft composer

- Łączy `text_raw + media metadata + kontekst kanału`.
- Generuje `draft_payload` dla OpenAI (już bez web search).

## 4) AI rewrite

OpenAI dostaje:

- treść źródłową,
- listę mediów (np. `[image: protest_warszawa_2026_04_08.jpg]`),
- zasady redakcyjne kanału,
- ograniczenia długości i styl.

OpenAI zwraca:

- `title_suggestion`,
- `body_rewritten`,
- `hashtags`,
- `risk_flags` (np. niepewne twierdzenia).

---

## Rola narzędzi: Bytebot vs n8n vs lokalny model ML

## n8n (zalecane jako rdzeń)

- Scheduler, retry, kolejki błędów, webhooki i alerty.
- Szybkie iteracje bez przebudowy backendu.
- Dobre do spinania API + własnych endpointów Django.

## Bytebot / Playwright (zalecany tylko jako fallback)

- Używaj tam, gdzie API nie daje pełnych danych.
- Dobre do „ostatniej mili” (np. wyciągnięcie pełnego galerii z publicznego posta).
- Wymaga większej kontroli anty-bot/compliance i monitoringu.

## Lokalny model ML (opcjonalny dodatek, nie główny collector)

- Przydatny do klasyfikacji, deduplikacji semantycznej, tagowania tematów.
- Nie zastępuje warstwy pobierania danych z platform.
- Można hostować lokalnie dla kosztów i prywatności (np. embeddings/reranking).

---

## Priorytety per platforma

## Telegram

1. Oficjalny bot/API dla własnych kanałów.
2. Telethon (client API) dla publicznych kanałów monitorowanych.
3. Zachowuj `message_id`, `media_group_id`, `caption_entities`.

## X

1. Oficjalne API (najlepsza jakość i zgodność).
2. Gdy brak API: controlled browser automation (Bytebot/Playwright), z limitem kont i mocnym monitoringiem.
3. Zawsze zapisuj `tweet_id/post_id`, `conversation_id`, `media_keys`, `expanded_urls`.

## Facebook

1. Graph API dla stron z uprawnieniami.
2. Unikaj scrapingu prywatnych profili i zamkniętych grup.
3. Zapisuj `post_id`, `permalink_url`, `attachments`, `message`, `created_time`.

---

## Plan wdrożenia (4 etapy)

## Etap 1: Quick win (1–2 tyg.)

- Włącz n8n i osobne workflow „collector per platforma”.
- Pobieraj raw posty + media do wspólnego storage.
- OpenAI tylko rewrite na gotowym payloadzie.

## Etap 2: Stabilizacja (2–4 tyg.)

- Deduplikacja po hashach mediów i podobieństwie tekstu.
- Retry policy per platforma + dead-letter queue.
- Dashboard jakości: `% postów z kompletem mediów`.

## Etap 3: Skalowanie (4–6 tyg.)

- Priorytetyzacja źródeł po jakości i świeżości.
- Lokalny model do klasyfikacji tematów i filtrowania spamu.
- Automatyczne fallbacki (API -> browser -> manual review).

## Etap 4: Compliance i audyt

- Rejestrowanie źródła każdego elementu draftu (`traceability`).
- Reguły retencji i usuwania danych.
- Audyt dostępu do tokenów/API keys.

---

## Minimalny kontrakt danych do OpenAI (rewrite-only)

Przykładowy payload:

```json
{
  "channel": "news_pl",
  "language": "pl",
  "source_items": [
    {
      "platform": "x",
      "post_id": "1890012345678901234",
      "text_raw": "...",
      "source_url": "https://x.com/...",
      "media": [
        {"type": "image", "storage_url": "s3://.../img1.jpg", "checksum": "sha256:..."}
      ],
      "published_at": "2026-04-09T10:22:00Z"
    }
  ],
  "editorial_rules": {
    "max_chars": 900,
    "tone": "konkretny, neutralny",
    "must_include_source": true
  }
}
```

Dzięki temu model nie „szuka” już posta samodzielnie, tylko redaguje dane, które system ma pod kontrolą.

---

## Najważniejsze KPI po zmianie

- `% draftów z pełnym kompletem mediów` (cel: >95%).
- `source-to-draft latency` (czas od publikacji źródła do draftu).
- `error rate per platforma` (API/scraping).
- `% draftów wymagających ręcznej korekty`.
- `koszt AI na 1 draft` po przejściu na rewrite-only.

## Decyzja końcowa

Jeśli celem jest **stabilny i skalowalny pipeline**, to rekomendacja jest:

- **n8n-first (natywne integracje Telegram/X/Facebook) + oficjalne API jako źródło prawdy + Bytebot tylko fallback + OpenAI wyłącznie rewrite**.

To podejście daje najwyższą jakość danych wejściowych, najmniejszą liczbę brakujących mediów i najlepszą kontrolę operacyjną.
