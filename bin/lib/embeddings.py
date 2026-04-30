"""Shared embedding infrastructure.

Computes embeddings via local ollama server, caches them on disk by content hash.
Used by:
- bin/update-graph-colors.py (color assignment for domains)
- (future) tiling/dedup checks, semantic search, related-suggestion

Cache file: wiki/meta/embeddings.json
Format: { "<sha256(model + text)>": [<float>, ...] }

Environment:
- OLLAMA_URL: defaults to http://127.0.0.1:11434
- OLLAMA_EMBED_MODEL: defaults to nomic-embed-text
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CACHE_PATH = Path("wiki/meta/embeddings.json")


def _cache_key(text: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(f"model={model}\n".encode("utf-8"))
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _load_cache() -> dict[str, list[float]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, separators=(",", ":"))
    tmp.replace(CACHE_PATH)


def _fetch_embedding(text: str, model: str) -> list[float]:
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to reach ollama at {OLLAMA_URL}. "
            f"Is the server running? `ollama serve` or `brew services start ollama`. ({e})"
        ) from e
    emb = data.get("embedding")
    if not emb:
        raise RuntimeError(f"ollama returned no embedding (model={model}, response={data!r})")
    return emb


def get_embedding(text: str, model: str | None = None) -> list[float]:
    """Get embedding for text, using cache when possible."""
    model = model or EMBED_MODEL
    key = _cache_key(text, model)

    cache = _load_cache()
    if key in cache:
        return cache[key]

    emb = _fetch_embedding(text, model)
    cache[key] = emb
    _save_cache(cache)
    return emb


def get_embeddings(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Batch version. Cache-aware: only fetches missing entries."""
    model = model or EMBED_MODEL
    cache = _load_cache()
    result = []
    new_entries = False
    for text in texts:
        key = _cache_key(text, model)
        if key in cache:
            result.append(cache[key])
        else:
            emb = _fetch_embedding(text, model)
            cache[key] = emb
            result.append(emb)
            new_entries = True
    if new_entries:
        _save_cache(cache)
    return result


def is_available() -> bool:
    """Check if ollama is reachable. Used for graceful fallback."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False
