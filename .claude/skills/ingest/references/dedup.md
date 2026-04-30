# Source-level дедуп

Перед каждым ingest источника — проверка через `raw/meta/ingested.json`. Чтобы повторный ingest того же файла или URL не запускал синтез заново.

## Структура `raw/meta/ingested.json`

```json
{
  "sources": {
    "raw/RLHF.md": {
      "hash": "<sha256 содержимого>",
      "ingested_at": "2026-04-29T15:45:00",
      "pages_created": ["wiki/ideas/RLHF.md", "wiki/ideas/PPO.md"],
      "pages_updated": ["wiki/index.md", "wiki/cache.md"]
    },
    "raw/articles/policy-gradient-2026-04-30.md": {
      "source_url": "https://lilianweng.github.io/posts/2018-04-08-policy-gradient/",
      "hash": "<sha256 тела без frontmatter>",
      "ingested_at": "2026-04-30T10:00:00",
      "pages_created": [...],
      "pages_updated": [...]
    }
  }
}
```

Поле `source_url` присутствует только у URL-источников. Хеш у URL-источников считается **только от тела** (без frontmatter с `fetched`-датой), чтобы тот же URL без изменений на странице давал тот же hash.

## Pre-ingest проверки

### Для файла-источника (path)

1. Если `raw/meta/ingested.json` отсутствует — создать `{"sources": {}}`.
2. Посчитать hash источника:
   - Markdown (`.md`) — `sha256sum raw/<path> | cut -d' ' -f1` — весь файл.
   - PDF / DOCX (бинарный формат) — `sha256sum raw/<path>.<ext>` (оригинал, не транскрипт). Транскрипт `.<ext>.md` — производный артефакт, его hash не имеет значения.
3. Найти запись по ключу `raw/<path>`. Если есть и `hash` совпадает — skip:
   ```
   Источник raw/RLHF.md уже обработан (без изменений с 2026-04-29).
   Используй `/ingest --force` чтобы пересинтезировать.
   ```
4. Иначе — для бинарных форматов: убедиться, что транскрипт `<file>.<ext>.md` существует и свежее оригинала (если нет — вызвать `python3 bin/transcribe.py raw/<path>.<ext>`). Затем продолжать synthesis по транскрипту.

### Для URL-источника

1. Прочитать `raw/meta/ingested.json`.
2. **Поиск по `source_url`**: пройти по всем записям в `sources`, найти запись с `source_url` == текущий URL.
3. Если запись найдена и файл по её ключу-пути всё ещё существует:
   - Скачать содержимое заново через defuddle (без сохранения).
   - Посчитать sha256 нового содержимого.
   - Сравнить с сохранённым `hash`:
     - Совпадает → skip ("уже обработан, страница не изменилась").
     - Различается → продолжать ingest, перезаписав файл.
4. Если записи нет — продолжать как новый источник.

Детали URL-flow — `references/url-ingestion.md`.

## Post-ingest запись

После Phase 8 записать/обновить запись в `raw/meta/ingested.json`:

```json
"raw/<path>": {
  "source_url": "<URL если был>",
  "hash": "<sha256 оригинала>",
  "transcript": "raw/<path>.<ext>.md",
  "ingested_at": "<ISO timestamp>",
  "pages_created": [<wiki paths>],
  "pages_updated": [<wiki paths>]
}
```

Поле `transcript` — путь к производному markdown-файлу (для PDF/DOCX). Для `.md`-источников и URL — отсутствует.

Записать файл целиком (атомарно).

## Force

`/ingest --force` пропускает все pre-ingest проверки. После успеха запись в manifest обновляется как обычно (новый hash).
