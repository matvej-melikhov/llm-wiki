# llm-wiki

Реализация паттерна LLM Wiki от Андрея Карпаты в формате Claude Code плагина и Obsidian vault.

## Идея

Вместо того чтобы каждый раз заново читать сырые документы (классический RAG), LLM строит и поддерживает структурированную базу знаний — wiki из markdown-страниц с перекрёстными ссылками. С каждым новым источником wiki становится богаче.

При запросе LLM не пересинтезирует знание из chunks — он читает уже готовые страницы, где синтез был выполнен один раз при ingestion.

## Источник паттерна

[Andrej Karpathy — LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

## Зависимости

**defuddle** — для URL-ingestion. Очищает веб-страницы от мусора и возвращает дословный markdown:

```bash
npm install -g defuddle
```

Без него `/ingest <url>` не работает (для file-ingestion из `raw/` defuddle не нужен).

**ollama** + `nomic-embed-text` — для семантической раскраски графа и (в будущем) других embedding-фич:

```bash
brew install ollama          # или https://ollama.com/download
ollama pull nomic-embed-text
```

Без ollama скрипт `bin/update-graph-colors.py` падает на hash-based раскраску (стабильно, но без семантической близости). Для других embedding-фич (semantic search, tiling) ollama обязательна.

**Python 3.10+** с `numpy`-стандартом — для скриптов в `bin/`. Используется только стандартная библиотека, дополнительные пакеты не требуются.

## Статус

В активной разработке.

## Лицензия

MIT
