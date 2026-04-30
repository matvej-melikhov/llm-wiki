#!/usr/bin/env python3
"""Update Obsidian graph view colors based on domain pages.

Reads `wiki/domains/*.md`, computes a stable hue for each domain, and writes
`colorGroups` into `.obsidian/graph.json`.

Two strategies:

1. **Embedding-based** (default if ollama is reachable):
   - Get embedding for each domain (name + first paragraph of description)
   - Project to 1D via fixed PCA basis stored in `wiki/meta/graph-colors-basis.json`
   - Map projection to hue. Stability guaranteed by frozen basis: new domains
     project onto existing axes, never recompute basis.

2. **Hash-based fallback** (if ollama unavailable or basis bootstrapping fails):
   - hue = sha256(domain_name) % 360
   - Stable, infinite, but no semantic meaning

In both cases: same domain name → same hue (for stable runs).

CAUTION: Obsidian overwrites .obsidian/graph.json when running. Close Obsidian
before running this script, OR use Obsidian's "Reload settings" command after.

Usage:
  python3 bin/update-graph-colors.py
  python3 bin/update-graph-colors.py --hash       # force hash-based
  python3 bin/update-graph-colors.py --dry-run    # show, don't write
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import embeddings  # type: ignore


DOMAINS_DIR = Path("wiki/domains")
GRAPH_PATH = Path(".obsidian/graph.json")
BASIS_PATH = Path("wiki/meta/graph-colors-basis.json")

# Color appearance (HSV → RGB). Tuned for Obsidian dark theme.
SATURATION = 0.65
VALUE = 0.9


def parse_domain(path: Path) -> tuple[str, str]:
    """Return (name, description). Description is the first non-frontmatter,
    non-heading paragraph — used as embedding input alongside the name."""
    text = path.read_text(encoding="utf-8")
    name = path.stem

    # Strip frontmatter
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :]

    # Find first paragraph that isn't a heading or HTML comment
    description_lines: list[str] = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            if description_lines:
                break
            continue
        if line.startswith("#") or line.startswith("<!--") or line.startswith("```") or line.startswith("![[") or line.startswith("|"):
            continue
        description_lines.append(line)
        if len(" ".join(description_lines)) > 400:
            break
    description = " ".join(description_lines).strip()

    return name, description


def discover_domains() -> list[tuple[str, str]]:
    """List (name, description) for every domain page. Sorted by name for stability."""
    if not DOMAINS_DIR.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for f in sorted(DOMAINS_DIR.glob("*.md")):
        name, desc = parse_domain(f)
        out.append((name, desc))
    return out


# ---------- Hash-based hue ----------


def hash_hue(name: str) -> float:
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return (int(h[:8], 16) % 36000) / 100.0  # 0..360, 0.01° resolution


# ---------- Embedding-based hue ----------


def _vec_sub(a: list[float], b: list[float]) -> list[float]:
    return [x - y for x, y in zip(a, b)]


def _vec_add(a: list[float], b: list[float]) -> list[float]:
    return [x + y for x, y in zip(a, b)]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(a: list[float]) -> float:
    return math.sqrt(_dot(a, a))


def _normalize(a: list[float]) -> list[float]:
    n = _norm(a)
    return [x / n for x in a] if n else a


def _power_iteration(vectors: list[list[float]], iters: int = 100) -> list[float]:
    """Find principal direction of covariance via power iteration. Returns unit vector."""
    if not vectors:
        return []
    dim = len(vectors[0])
    # Center
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    centered = [_vec_sub(v, mean) for v in vectors]

    # Random init
    rng = hashlib.sha256(b"graph-colors-pca").digest()
    x = [((rng[i % len(rng)] / 255.0) - 0.5) for i in range(dim)]
    x = _normalize(x)

    for _ in range(iters):
        # x = (X^T X) x  → equivalent to summing v · (v · x) over v in centered
        new_x = [0.0] * dim
        for v in centered:
            coef = _dot(v, x)
            for i in range(dim):
                new_x[i] += coef * v[i]
        x = _normalize(new_x)
    return x


def load_basis() -> tuple[list[float], list[float]] | None:
    """Return (mean, axis) if persisted, else None."""
    if not BASIS_PATH.exists():
        return None
    try:
        d = json.loads(BASIS_PATH.read_text(encoding="utf-8"))
        return d["mean"], d["axis"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def save_basis(mean: list[float], axis: list[float], hue_offset: float) -> None:
    BASIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASIS_PATH.write_text(
        json.dumps(
            {"mean": mean, "axis": axis, "hue_offset": hue_offset},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_hue_offset() -> float:
    if not BASIS_PATH.exists():
        return 0.0
    try:
        return json.loads(BASIS_PATH.read_text(encoding="utf-8")).get("hue_offset", 0.0)
    except (json.JSONDecodeError, OSError):
        return 0.0


def embedding_hues(domains: list[tuple[str, str]]) -> dict[str, float]:
    """Compute embedding-based hues for all domains. Bootstraps or reuses basis."""
    texts = [f"{name}. {desc}" if desc else name for name, desc in domains]
    vecs = embeddings.get_embeddings(texts)

    basis = load_basis()
    if basis is None:
        # Bootstrap: compute mean + first principal axis from current domains
        if len(vecs) < 2:
            # One domain — no axis to compute; degrade to hash
            raise RuntimeError("need ≥2 domains to bootstrap embedding basis")
        mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(len(vecs[0]))]
        axis = _power_iteration(vecs)
        # Pick hue_offset such that the first domain alphabetically gets hue=0.
        # This makes first added domain reproducibly the "0° anchor".
        first_proj = _dot(_vec_sub(vecs[0], mean), axis)
        # Compute projections for normalization range
        projs = [_dot(_vec_sub(v, mean), axis) for v in vecs]
        # Map projection range to [0, 360); we'll use sigmoid-like normalization
        # so future domains outside the bootstrap range still land somewhere sensible
        save_basis(mean, axis, hue_offset=-first_proj)
        basis = (mean, axis)

    mean, axis = basis
    hue_offset = load_hue_offset()

    hues: dict[str, float] = {}
    for (name, _desc), vec in zip(domains, vecs):
        proj = _dot(_vec_sub(vec, mean), axis) + hue_offset
        # Map (-∞, ∞) → [0, 360) via sigmoid scaled to full hue range.
        # The scale factor 5.0 spreads typical projection range across most of [0, 360).
        sigmoid = 1.0 / (1.0 + math.exp(-proj / 5.0))
        hues[name] = sigmoid * 360.0
    return hues


# ---------- Color assignment ----------


def hue_to_rgb_int(hue: float, saturation: float = SATURATION, value: float = VALUE) -> int:
    r, g, b = colorsys.hsv_to_rgb(hue / 360.0, saturation, value)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def count_domain_pages(name: str) -> int:
    """Count pages whose `domain` frontmatter contains this domain name.
    Used to order color groups from leaf (specific) to root (general)."""
    count = 0
    for folder in ("ideas", "entities", "questions"):
        d = Path("wiki") / folder
        if not d.is_dir():
            continue
        needle = f'"[[{name}]]"'
        for f in d.glob("*.md"):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            # Crude but effective: look for the wikilink in frontmatter region
            if "---" in text:
                fm_end = text.find("\n---", 3)
                if fm_end > 0 and needle in text[:fm_end]:
                    count += 1
    return count


def build_color_groups(hues: dict[str, float]) -> list[dict]:
    """Order: leaf domains first (smaller page count), root domains last.
    Obsidian matches the first colorGroup whose query matches a page, so
    putting more-specific domains first means a page in both ML and RL
    gets the RL color (more specific) instead of the ML color (parent)."""
    domain_counts = [(name, count_domain_pages(name)) for name in hues]
    domain_counts.sort(key=lambda x: (x[1], x[0]))  # asc by count, then by name

    groups = []
    for name, _count in domain_counts:
        rgb = hue_to_rgb_int(hues[name])
        groups.append({
            "query": f'["domain":"{name}"]',
            "color": {"a": 1, "rgb": rgb},
        })
    return groups


# ---------- Main ----------


def update_graph_json(color_groups: list[dict], dry_run: bool = False) -> None:
    if not GRAPH_PATH.exists():
        print(f"warning: {GRAPH_PATH} not found, creating", file=sys.stderr)
        graph = {}
    else:
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))

    graph["colorGroups"] = color_groups

    if dry_run:
        print(json.dumps(graph, ensure_ascii=False, indent=2))
        return

    GRAPH_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hash", action="store_true", help="force hash-based hues, skip embeddings")
    ap.add_argument("--dry-run", action="store_true", help="print result, don't write")
    args = ap.parse_args()

    domains = discover_domains()
    if not domains:
        print("no domain pages in wiki/domains/, nothing to do", file=sys.stderr)
        return 0

    use_embeddings = not args.hash and embeddings.is_available()
    if not use_embeddings and not args.hash:
        print("warning: ollama unavailable, falling back to hash-based hues", file=sys.stderr)

    if use_embeddings:
        try:
            hues = embedding_hues(domains)
            mode = "embedding"
        except Exception as e:
            print(f"warning: embedding mode failed ({e}), falling back to hash", file=sys.stderr)
            hues = {name: hash_hue(name) for name, _ in domains}
            mode = "hash (fallback)"
    else:
        hues = {name: hash_hue(name) for name, _ in domains}
        mode = "hash"

    groups = build_color_groups(hues)
    update_graph_json(groups, dry_run=args.dry_run)

    print(f"mode: {mode}, domains: {len(groups)}")
    for name in sorted(hues):
        rgb = hue_to_rgb_int(hues[name])
        print(f"  {name:30s}  hue={hues[name]:6.1f}°  rgb=#{rgb:06x}")

    if not args.dry_run:
        print("\nNote: if Obsidian was open during this run, your changes may be")
        print("overwritten. Close Obsidian → re-run, or use 'Reload app and plugins'")
        print("inside Obsidian after this script.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
