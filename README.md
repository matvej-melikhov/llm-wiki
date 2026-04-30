# llm-wiki

Реализация паттерна LLM Wiki от Андрея Карпаты в формате Claude Code плагина и Obsidian vault.

## Идея

Вместо того чтобы каждый раз заново читать сырые документы (классический RAG), LLM строит и поддерживает структурированную базу знаний — wiki из markdown-страниц с перекрёстными ссылками. С каждым новым источником wiki становится богаче.

При запросе LLM не пересинтезирует знание из chunks — он читает уже готовые страницы, где синтез был выполнен один раз при ingestion.

## Источник паттерна

[Andrej Karpathy — LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

## Установка

Все зависимости ставятся одной командой:

```bash
bash bin/setup.sh
```

Что внутри:

| Зависимость | Для чего | Команда установки |
|---|---|---|
| [defuddle](https://github.com/kepano/defuddle) | URL ingestion | `npm install -g defuddle` |
| [pandoc](https://pandoc.org/) | DOCX → markdown | `brew install pandoc` |
| [pymupdf4llm](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) | PDF → markdown | `pip3 install --user pymupdf4llm` |

Скрипт `bin/setup.sh` написан под macOS (Homebrew). На Linux замени `brew install` на `apt install` / `dnf install`. Зависимости не критические — `/ingest` markdown-файлов и изображений работает и без них.

## Статус

В активной разработке.

## Лицензия

MIT
