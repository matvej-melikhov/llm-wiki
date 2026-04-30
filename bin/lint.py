#!/usr/bin/env python3
"""Programmatic lint for the wiki.

Implements deterministic checks that don't need LLM judgment. Outputs a
structured `wiki/meta/lint-state.json` with `open_issues`. The semantic
checks (contradiction, outdated-claim, missing-concept, style-nit) stay
agent-driven — they're done in a separate LLM-fueled pass via the lint
skill (Phase 8 of ingest).

This script is the "first layer" of two-layer lint:
1. bin/lint.py — fast, deterministic, ~all schema/structural checks
2. lint skill — LLM pass for semantic checks, reads lint-state.json
   from us, adds its own `open_issues` entries.

Usage:
    python3 bin/lint.py            # run, write lint-state.json, print summary
    python3 bin/lint.py --force    # bypass aggregate-hash skip-check
    python3 bin/lint.py --json     # also print full open_issues JSON
    python3 bin/lint.py --check <type>   # run only one check (debugging)

Exit codes:
    0 — clean (no issues, or skipped)
    1 — issues found (just informational; not a hard failure)
    2 — internal error
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ────────────────────────────────────────────────────────────────────────
# Paths and constants
# ────────────────────────────────────────────────────────────────────────

WIKI_ROOT = Path("wiki")
LINT_STATE_PATH = WIKI_ROOT / "meta" / "lint-state.json"

# Folder ↔ frontmatter `type:` mapping. Folder is source of truth.
FOLDER_TO_TYPE = {
    "ideas": "idea",
    "entities": "entity",
    "questions": "question",
    "domains": "domain",
}
CONTENT_FOLDERS = list(FOLDER_TO_TYPE.keys())

VALID_STATUSES = {"evaluation", "in-progress", "ready"}
LEGACY_FIELDS = {"title", "complexity", "first_mentioned"}

# Tag casing rules: known abbreviations stay uppercase, regular words get
# Capitalized first letter. We can't enumerate all valid abbreviations, so
# we use heuristics.
KNOWN_ABBREVIATIONS = {
    "ML", "RL", "NLP", "RLHF", "LLM", "LoRA", "GAN", "VAE", "MOC",
    "AI", "DL", "CV", "NN", "GPT", "BERT", "API", "URL", "HTML",
    "JSON", "YAML", "PDF", "PPO", "DPO", "RM", "KL", "TD", "MC",
    "GAE", "TRPO", "MAP", "MRR", "DCG", "ERR", "IR", "QA", "GBDT",
    "MDI", "FI", "PFound", "OCR",
}


# ────────────────────────────────────────────────────────────────────────
# Frontmatter parser
# ────────────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Frontmatter:
    """Parsed frontmatter. Keeps raw text for round-trip preservation."""
    raw: str                     # text between --- markers, no markers
    fields: dict[str, Any]       # parsed values: str | list | None
    inline_lists: set[str]       # names of fields written as `[a, b]` inline
    end_line: int                # line number where second --- closes (0-indexed)


def _parse_yaml_subset(text: str) -> tuple[dict[str, Any], set[str]]:
    """Parse a restricted subset of YAML matching our frontmatter schema:
    - top-level keys with string or list values
    - lists either block-style (newline + `  - item`) or inline `[a, b]`
    - quoted strings: `"..."` or `'...'`
    - bare strings until end of line

    Returns (fields, inline_list_field_names).

    Limitations: no nested structures, no multiline strings, no anchors.
    Our schema doesn't use any of these.
    """
    fields: dict[str, Any] = {}
    inline_lists: set[str] = set()

    lines = text.split("\n")
    i = 0
    current_key: str | None = None
    current_list: list[str] | None = None

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Skip blank lines and comments
        if not stripped or stripped.lstrip().startswith("#"):
            i += 1
            continue

        # Block-list item: starts with whitespace + `-`
        list_item_match = re.match(r"^\s+-\s*(.*)$", line)
        if list_item_match and current_list is not None:
            value = list_item_match.group(1).strip()
            current_list.append(_strip_quotes(value))
            i += 1
            continue

        # If we hit a non-list line while collecting a list, finalize it
        if current_key is not None and current_list is not None:
            fields[current_key] = current_list
            current_key = None
            current_list = None

        # Top-level key: value
        kv_match = re.match(r"^([a-z_][a-z_0-9]*)\s*:\s*(.*)$", line, re.IGNORECASE)
        if not kv_match:
            i += 1
            continue
        key = kv_match.group(1)
        value_str = kv_match.group(2).strip()

        if value_str == "":
            # Block list or null — peek ahead to see
            if i + 1 < len(lines) and re.match(r"^\s+-\s*", lines[i + 1]):
                current_key = key
                current_list = []
            else:
                fields[key] = None
            i += 1
            continue

        if value_str.startswith("[") and value_str.endswith("]"):
            # Inline list
            inline_content = value_str[1:-1].strip()
            if not inline_content:
                fields[key] = []
            else:
                items = [_strip_quotes(x.strip()) for x in inline_content.split(",")]
                fields[key] = items
            inline_lists.add(key)
            i += 1
            continue

        # Scalar string
        fields[key] = _strip_quotes(value_str)
        i += 1

    # Finalize trailing list
    if current_key is not None and current_list is not None:
        fields[current_key] = current_list

    return fields, inline_lists


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def parse_frontmatter(text: str) -> tuple[Frontmatter | None, str]:
    """Split a markdown file into (Frontmatter, body). Returns (None, text)
    if no frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    raw = m.group(1)
    fields, inline_lists = _parse_yaml_subset(raw)
    end_line = text[: m.end()].count("\n")
    body = text[m.end() :]
    return Frontmatter(raw=raw, fields=fields, inline_lists=inline_lists, end_line=end_line), body


# ────────────────────────────────────────────────────────────────────────
# Page model
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Page:
    """One markdown page in the wiki."""
    path: Path                     # relative path from project root, e.g. "wiki/ideas/RLHF.md"
    folder: str                    # one of CONTENT_FOLDERS, or "meta", or "" for root
    name: str                      # filename without .md extension
    text: str                      # full content
    fm: Frontmatter | None         # parsed frontmatter (None if absent)
    body: str                      # text after frontmatter

    @property
    def page_type(self) -> str | None:
        """Value of `type:` field, or None."""
        if self.fm is None:
            return None
        return self.fm.fields.get("type")

    def relpath(self) -> str:
        """Path as POSIX string for issue reporting."""
        return self.path.as_posix()


# ────────────────────────────────────────────────────────────────────────
# Vault loading
# ────────────────────────────────────────────────────────────────────────


def discover_pages() -> list[Page]:
    """Walk wiki/ and return all .md pages. Excludes wiki/meta/ files
    (they're infrastructure, not content)."""
    pages: list[Page] = []
    if not WIKI_ROOT.is_dir():
        return pages

    for md in sorted(WIKI_ROOT.rglob("*.md")):
        # skip lint reports
        if md.parent.name == "meta" and md.name.startswith("lint-report-"):
            continue
        # determine folder
        rel = md.relative_to(WIKI_ROOT)
        if len(rel.parts) == 1:
            folder = ""
        else:
            folder = rel.parts[0]

        text = md.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        pages.append(Page(
            path=md,
            folder=folder,
            name=md.stem,
            text=text,
            fm=fm,
            body=body,
        ))
    return pages


# ────────────────────────────────────────────────────────────────────────
# Skip-check via aggregate hash
# ────────────────────────────────────────────────────────────────────────


def compute_wiki_hash(pages: list[Page]) -> str:
    """Aggregate sha256 over all wiki pages' content.

    Sort by path to make order-independent. Concatenate contents. One hash.
    Excludes lint-state.json itself and lint-report files (they're outputs).
    """
    h = hashlib.sha256()
    for p in sorted(pages, key=lambda p: p.path.as_posix()):
        h.update(p.path.as_posix().encode("utf-8"))
        h.update(b"\n")
        h.update(p.text.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def load_lint_state() -> dict[str, Any]:
    if not LINT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(LINT_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_lint_state(state: dict[str, Any]) -> None:
    LINT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINT_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────────────────
# Issue collection
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Issue:
    """A single open issue to report."""
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.payload}


# ────────────────────────────────────────────────────────────────────────
# Checks
# ────────────────────────────────────────────────────────────────────────
#
# Each check function takes a list[Page] (and optionally pre-built indexes)
# and yields Issue objects. Checks are pure — no I/O.
# ────────────────────────────────────────────────────────────────────────


def check_status_not_in_enum(pages: list[Page]) -> Iterable[Issue]:
    """status: must be one of evaluation/in-progress/ready (when present and
    not on entity)."""
    for p in pages:
        if p.fm is None:
            continue
        status = p.fm.fields.get("status")
        if status is None:
            continue
        # entity has its own check that says status shouldn't even be there
        if p.page_type == "entity":
            continue
        if status not in VALID_STATUSES:
            yield Issue("status-not-in-enum", {
                "where": p.relpath(),
                "value": status,
                "fix": "in-progress",
            })


def check_status_on_entity(pages: list[Page]) -> Iterable[Issue]:
    """type: entity pages must not have a status field."""
    for p in pages:
        if p.fm is None:
            continue
        if p.page_type != "entity":
            continue
        if "status" in p.fm.fields:
            yield Issue("status-on-entity", {"where": p.relpath()})


def check_legacy_field(pages: list[Page]) -> Iterable[Issue]:
    """Old-schema fields (title/complexity/first_mentioned) on non-meta pages."""
    for p in pages:
        if p.fm is None:
            continue
        if p.page_type == "meta":
            continue
        for field_name in LEGACY_FIELDS:
            if field_name in p.fm.fields:
                yield Issue("legacy-field", {
                    "where": p.relpath(),
                    "field": field_name,
                })


def _expected_tag_casing(tag: str) -> str | None:
    """Return the canonically-cased version of `tag`, or None if it already
    matches schema rules.

    Schema (from references/frontmatter.md):
    - abbreviations uppercase: ML, RL, NLP, RLHF, ...
    - regular words capitalized: Alignment, Optimization, ...
    - no lowercase abbreviations, no mixed-case abbreviations

    Heuristic (without hardcoding every possible abbreviation):
    - tag is all-uppercase → already canonical abbreviation, accept
    - tag in KNOWN_ABBREVIATIONS list (case-insensitive) → fix to canonical form
      (handles LoRA, GPT-3, etc. with non-trivial casing)
    - tag starts with uppercase letter (rest mixed/lower) → accept
      (Capitalized "Alignment", or mixed like "KMeans", "MapReduce")
    - tag starts with lowercase → fix: capitalize first letter, lowercase rest

    This is permissive: any tag that *could* be a valid abbreviation or
    capitalized word passes. It only flags clear violations like 'ml' or
    'optimization'.
    """
    if not tag:
        return None

    # All-uppercase: canonical abbreviation form
    if tag.isupper():
        return None

    # Known abbreviation with non-trivial casing (LoRA → keep as LoRA)
    upper = tag.upper()
    for a in KNOWN_ABBREVIATIONS:
        if a.upper() == upper:
            return None if tag == a else a

    # Starts with uppercase letter: accept (Capitalized regular word, or
    # mixed-case like KMeans we can't validate further without a dictionary)
    if tag[0].isupper():
        return None

    # Starts with lowercase: violation. Suggest Capitalized form as fix.
    expected = tag[0].upper() + tag[1:].lower()
    return expected


def check_lowercase_tags(pages: list[Page]) -> Iterable[Issue]:
    """Tags must follow casing schema: abbreviations uppercase
    (ML/RL/NLP), regular words Capitalized."""
    for p in pages:
        if p.fm is None:
            continue
        tags = p.fm.fields.get("tags")
        if not tags or not isinstance(tags, list):
            continue
        bad: list[str] = []
        for t in tags:
            if not isinstance(t, str):
                continue
            if _expected_tag_casing(t) is not None:
                bad.append(t)
        if bad:
            yield Issue("lowercase-tags", {
                "where": p.relpath(),
                "tags": bad,
            })


def check_inline_tags(pages: list[Page]) -> Iterable[Issue]:
    """tags: [a, b] inline format instead of block-style YAML.

    Empty inline lists (tags: []) are accepted — that's a normal placeholder
    in templates. We only flag inline lists with at least one element."""
    for p in pages:
        if p.fm is None:
            continue
        if "tags" not in p.fm.inline_lists:
            continue
        tags = p.fm.fields.get("tags")
        if isinstance(tags, list) and len(tags) > 0:
            yield Issue("inline-tags", {"where": p.relpath()})


def check_folder_type_mismatch(pages: list[Page]) -> Iterable[Issue]:
    """Page in wiki/<X>/ must have type: matching the folder."""
    for p in pages:
        if p.fm is None:
            continue
        expected = FOLDER_TO_TYPE.get(p.folder)
        if expected is None:
            continue  # not in a content folder (e.g. meta or wiki root)
        current = p.page_type
        if current != expected:
            yield Issue("folder-type-mismatch", {
                "where": p.relpath(),
                "current_type": current,
                "expected_type": expected,
            })


# Registry: ordered list of (issue_type_string, check_function)
_CHECKS: list[tuple[str, Any]] = [
    ("status-not-in-enum", check_status_not_in_enum),
    ("status-on-entity", check_status_on_entity),
    ("legacy-field", check_legacy_field),
    ("lowercase-tags", check_lowercase_tags),
    ("inline-tags", check_inline_tags),
    ("folder-type-mismatch", check_folder_type_mismatch),
]


def run_all_checks(pages: list[Page], filter_type: str | None = None) -> list[Issue]:
    """Run every registered check. If filter_type is set, run only that one."""
    issues: list[Issue] = []
    for type_name, check_fn in _CHECKS:
        if filter_type and type_name != filter_type:
            continue
        issues.extend(check_fn(pages))
    return issues


# ────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="bypass aggregate-hash skip-check")
    ap.add_argument("--json", action="store_true", help="print full open_issues JSON to stdout")
    ap.add_argument("--check", type=str, default=None, help="run only one check by type")
    args = ap.parse_args()

    pages = discover_pages()
    if not pages:
        print("no wiki pages found", file=sys.stderr)
        return 0

    wiki_hash = compute_wiki_hash(pages)
    state = load_lint_state()

    # Skip-check
    if not args.force:
        if state.get("wiki_hash") == wiki_hash and not state.get("open_issues"):
            print(f"wiki unchanged since last audit ({state.get('last_audit')}). clean. skipping.")
            return 0

    issues = run_all_checks(pages, filter_type=args.check)

    new_state = {
        "wiki_hash": wiki_hash,
        "last_audit": dt.datetime.now().isoformat(timespec="seconds"),
        "files_checked": len(pages),
        "open_issues": [iss.to_dict() for iss in issues],
    }
    save_lint_state(new_state)

    print(f"checked {len(pages)} pages, found {len(issues)} open issues")
    if args.json:
        print(json.dumps(new_state, ensure_ascii=False, indent=2))

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
