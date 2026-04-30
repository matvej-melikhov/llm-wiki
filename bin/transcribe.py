#!/usr/bin/env python3
"""Universal source transcriber.

Converts non-markdown source files (PDF, DOCX, audio, video) into markdown
transcripts suitable for ingest. Output goes next to the original with `.md`
appended:

  raw/article.pdf       → raw/article.pdf.md
  raw/notes.docx        → raw/notes.docx.md
  raw/lecture.mp3       → raw/lecture.mp3.md   (future)
  raw/recording.mp4     → raw/recording.mp4.md (future)

Usage:
  python3 bin/transcribe.py <path>
  python3 bin/transcribe.py <path> --force   # overwrite existing transcript

Idempotent: if `<path>.md` already exists and is newer than original, exits
quickly. Use --force to re-transcribe.

Dependencies (install via bin/setup.sh):
  - pandoc          for .docx
  - pymupdf4llm     for .pdf
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path


SUPPORTED_EXTENSIONS = {".pdf", ".docx"}
# Future: ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm"


def transcribe_pdf(src: Path) -> str:
    """PDF → markdown via pymupdf4llm. Preserves tables, headings, basic
    formatting. Math sometimes survives, often degrades to text. Works for
    arbitrary page counts."""
    try:
        import pymupdf4llm  # type: ignore
    except ImportError:
        sys.exit(
            "pymupdf4llm not installed. Run:\n"
            "  pip3 install --user pymupdf4llm\n"
            "or run bin/setup.sh"
        )
    return pymupdf4llm.to_markdown(str(src))


def transcribe_docx(src: Path) -> str:
    """DOCX → markdown via pandoc. Preserves tables, headings, lists,
    inline formatting."""
    if not shutil.which("pandoc"):
        sys.exit(
            "pandoc not installed. Run:\n"
            "  brew install pandoc   # macOS\n"
            "  apt install pandoc    # Debian/Ubuntu\n"
            "or run bin/setup.sh"
        )
    result = subprocess.run(
        ["pandoc", "--from=docx", "--to=gfm", "--wrap=none", str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"pandoc failed: {result.stderr}")
    return result.stdout


TRANSCRIBERS = {
    ".pdf": transcribe_pdf,
    ".docx": transcribe_docx,
}


def needs_update(src: Path, dest: Path, force: bool) -> bool:
    """True if dest doesn't exist or is older than src, or --force."""
    if force:
        return True
    if not dest.exists():
        return True
    return src.stat().st_mtime > dest.stat().st_mtime


def build_frontmatter(src: Path) -> str:
    ext = src.suffix.lower().lstrip(".")
    today = dt.datetime.now().isoformat(timespec="seconds")
    return (
        "---\n"
        f"source_type: {ext}\n"
        f"original_file: {src.name}\n"
        f"transcribed_at: {today}\n"
        "---\n\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="path to source file")
    ap.add_argument("--force", action="store_true", help="overwrite existing transcript")
    args = ap.parse_args()

    src: Path = args.source
    if not src.is_file():
        sys.exit(f"not a file: {src}")

    ext = src.suffix.lower()
    if ext not in TRANSCRIBERS:
        supported = ", ".join(sorted(TRANSCRIBERS.keys()))
        sys.exit(f"unsupported extension: {ext}. supported: {supported}")

    dest = src.with_suffix(src.suffix + ".md")
    if not needs_update(src, dest, args.force):
        print(f"up-to-date: {dest}")
        return 0

    print(f"transcribing: {src}  →  {dest}")
    body = TRANSCRIBERS[ext](src)
    dest.write_text(build_frontmatter(src) + body, encoding="utf-8")
    size_kb = dest.stat().st_size / 1024
    print(f"done: {dest} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
