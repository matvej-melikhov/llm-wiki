"""Unit tests for bin/lint.py — Layer 1 deterministic lint.

Coverage: all 16 registered checks + frontmatter parser + wikilink extractor.

Design notes:
- Every check function is pure (list[Page] → Iterable[Issue]) — no I/O needed
  for 13 of 16 checks. Tests build Page objects directly via make_page().
- Three filesystem-dependent checks (stale-index, missing-index,
  binary-source) use pytest tmp_path + monkeypatch on module-level constants.
- "Clean" case + "violation" case + key edge cases for each check.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import pytest

# Make `bin/` importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lint as L
from lint import (
    Issue, Page,
    _expected_tag_casing,
    _extract_wikilinks,
    _normalize_wikilink_text,
    _parse_index_tables,
    _parse_yaml_subset,
    _wikilink_to_raw_key,
    check_asymmetric_related,
    check_binary_source_outside_formats,
    check_dangling_domain_ref,
    check_dead_link,
    check_empty_section,
    check_folder_type_mismatch,
    check_inline_tags,
    check_legacy_field,
    check_lowercase_tags,
    check_missing_index_entry,
    check_orphan,
    check_raw_link_with_extension,
    check_raw_ref_in_body,
    check_similar_but_unlinked,
    check_stale_index_entry,
    check_status_not_in_enum,
    check_status_on_entity,
    check_synthesis_drift,
    compute_wiki_hash,
    parse_frontmatter,
)

# Make embed module importable in tests
import embed as E
from embed import EmbedIndex


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def make_page(
    folder: str = "ideas",
    name: str = "Test-Page",
    fm_yaml: str = "",
    body: str = "",
) -> Page:
    """Build a Page from raw YAML + body text without filesystem access."""
    text = f"---\n{fm_yaml}\n---\n{body}" if fm_yaml else body
    fm, parsed_body = parse_frontmatter(text)
    path = (
        Path(f"wiki/{folder}/{name}.md") if folder else Path(f"wiki/{name}.md")
    )
    return Page(
        path=path,
        folder=folder,
        name=name,
        text=text,
        fm=fm,
        body=parsed_body,
    )


def issues_of(result) -> list[Issue]:
    return list(result)


def types_of(result) -> list[str]:
    return [i.type for i in issues_of(result)]


# ────────────────────────────────────────────────────────────────────────
# Frontmatter parser
# ────────────────────────────────────────────────────────────────────────


class TestParseFrontmatter:
    def test_basic_fields(self):
        text = "---\ntype: idea\nstatus: ready\n---\nbody"
        fm, body = parse_frontmatter(text)
        assert fm is not None
        assert fm.fields["type"] == "idea"
        assert fm.fields["status"] == "ready"
        assert body.strip() == "body"

    def test_block_list(self):
        text = "---\ntags:\n  - ML\n  - RL\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm.fields["tags"] == ["ML", "RL"]

    def test_inline_list(self):
        text = "---\ntags: [ML, RL]\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm.fields["tags"] == ["ML", "RL"]
        assert "tags" in fm.inline_lists

    def test_quoted_wikilinks(self):
        text = '---\nrelated:\n  - "[[RLHF]]"\n---\n'
        fm, _ = parse_frontmatter(text)
        assert fm.fields["related"] == ["[[RLHF]]"]

    def test_no_frontmatter(self):
        text = "# Just a heading\n\nsome text"
        fm, body = parse_frontmatter(text)
        assert fm is None
        assert "heading" in body

    def test_empty_list(self):
        text = "---\ntags: []\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm.fields["tags"] == []

    def test_null_value(self):
        text = "---\ndomain:\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm.fields["domain"] is None


# ────────────────────────────────────────────────────────────────────────
# Tag casing heuristic
# ────────────────────────────────────────────────────────────────────────


class TestExpectedTagCasing:
    def test_all_uppercase_ok(self):
        assert _expected_tag_casing("ML") is None
        assert _expected_tag_casing("NLP") is None
        assert _expected_tag_casing("RLHF") is None

    def test_known_abbreviation_correct_case(self):
        assert _expected_tag_casing("LoRA") is None

    def test_known_abbreviation_wrong_case_returns_canonical(self):
        result = _expected_tag_casing("ml")
        assert result == "ML"

    def test_capitalized_word_ok(self):
        assert _expected_tag_casing("Alignment") is None
        assert _expected_tag_casing("Optimization") is None

    def test_lowercase_word_flagged(self):
        result = _expected_tag_casing("alignment")
        assert result is not None
        assert result[0].isupper()

    def test_mixed_case_starts_uppercase_ok(self):
        # KMeans, MapReduce — can't tell without a dictionary, accept
        assert _expected_tag_casing("KMeans") is None

    def test_empty_tag(self):
        assert _expected_tag_casing("") is None


# ────────────────────────────────────────────────────────────────────────
# Wikilink extraction
# ────────────────────────────────────────────────────────────────────────


class TestExtractWikilinks:
    def test_basic_link(self):
        assert _extract_wikilinks("See [[RLHF]] for details") == ["RLHF"]

    def test_aliased_link(self):
        assert _extract_wikilinks("[[RLHF|Reinforcement Learning]]") == ["RLHF"]

    def test_anchor_link(self):
        assert _extract_wikilinks("[[RLHF#section]]") == ["RLHF"]

    def test_embed_excluded(self):
        assert _extract_wikilinks("![[image.png]]") == []

    def test_fenced_code_block_stripped(self):
        text = "```\n[[should-be-ignored]]\n```\noutside"
        cleaned = _normalize_wikilink_text(text)
        links = _extract_wikilinks(cleaned)
        assert "should-be-ignored" not in links

    def test_inline_code_stripped(self):
        text = "Use `[[not-a-link]]` in code"
        cleaned = _normalize_wikilink_text(text)
        assert "not-a-link" not in _extract_wikilinks(cleaned)

    def test_escaped_pipe_in_table(self):
        # In markdown tables, | is escaped as \|
        text = r"[[Page\|Alias]]"
        links = _extract_wikilinks(text)
        # After normalization, the pipe is real — link should be extracted
        assert len(links) >= 1

    def test_multiple_links(self):
        links = _extract_wikilinks("[[A]] and [[B]] and [[C]]")
        assert set(links) == {"A", "B", "C"}


# ────────────────────────────────────────────────────────────────────────
# check_status_not_in_enum
# ────────────────────────────────────────────────────────────────────────


class TestCheckStatusNotInEnum:
    def test_valid_statuses_ok(self):
        for s in ("evaluation", "in-progress", "ready"):
            p = make_page(fm_yaml=f"type: idea\nstatus: {s}")
            assert types_of(check_status_not_in_enum([p])) == []

    def test_invalid_status_flagged(self):
        p = make_page(fm_yaml="type: idea\nstatus: stable")
        issues = issues_of(check_status_not_in_enum([p]))
        assert len(issues) == 1
        assert issues[0].type == "status-not-in-enum"
        assert issues[0].payload["value"] == "stable"

    def test_entity_skipped(self):
        # entity status is handled by check_status_on_entity instead
        p = make_page(folder="entities", fm_yaml="type: entity\nstatus: stable")
        assert types_of(check_status_not_in_enum([p])) == []

    def test_no_status_field_ok(self):
        p = make_page(fm_yaml="type: idea")
        assert types_of(check_status_not_in_enum([p])) == []

    def test_fix_suggestion_present(self):
        p = make_page(fm_yaml="type: idea\nstatus: done")
        issues = issues_of(check_status_not_in_enum([p]))
        assert "fix" in issues[0].payload


# ────────────────────────────────────────────────────────────────────────
# check_status_on_entity
# ────────────────────────────────────────────────────────────────────────


class TestCheckStatusOnEntity:
    def test_entity_without_status_ok(self):
        p = make_page(folder="entities", fm_yaml="type: entity\nentity_type: person")
        assert types_of(check_status_on_entity([p])) == []

    def test_entity_with_status_flagged(self):
        p = make_page(folder="entities", fm_yaml="type: entity\nstatus: ready")
        issues = issues_of(check_status_on_entity([p]))
        assert len(issues) == 1
        assert issues[0].type == "status-on-entity"

    def test_non_entity_ignored(self):
        p = make_page(fm_yaml="type: idea\nstatus: ready")
        assert types_of(check_status_on_entity([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_legacy_field
# ────────────────────────────────────────────────────────────────────────


class TestCheckLegacyField:
    def test_no_legacy_fields_ok(self):
        p = make_page(fm_yaml="type: idea\nstatus: ready")
        assert types_of(check_legacy_field([p])) == []

    def test_title_field_flagged(self):
        p = make_page(fm_yaml='type: idea\ntitle: "My Idea"')
        issues = issues_of(check_legacy_field([p]))
        assert any(i.payload["field"] == "title" for i in issues)

    def test_complexity_field_flagged(self):
        p = make_page(fm_yaml="type: idea\ncomplexity: high")
        issues = issues_of(check_legacy_field([p]))
        assert any(i.payload["field"] == "complexity" for i in issues)

    def test_first_mentioned_flagged(self):
        p = make_page(fm_yaml="type: entity\nfirst_mentioned: 2024-01-01")
        issues = issues_of(check_legacy_field([p]))
        assert any(i.payload["field"] == "first_mentioned" for i in issues)

    def test_meta_page_exempt(self):
        p = make_page(folder="meta", fm_yaml='type: meta\ntitle: "Cache"')
        assert types_of(check_legacy_field([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_lowercase_tags
# ────────────────────────────────────────────────────────────────────────


class TestCheckLowercaseTags:
    def test_correct_tags_ok(self):
        p = make_page(fm_yaml="type: idea\ntags:\n  - ML\n  - Alignment")
        assert types_of(check_lowercase_tags([p])) == []

    def test_lowercase_tag_flagged(self):
        p = make_page(fm_yaml="type: idea\ntags:\n  - ml\n  - rl")
        issues = issues_of(check_lowercase_tags([p]))
        assert len(issues) == 1
        assert issues[0].type == "lowercase-tags"
        assert "ml" in issues[0].payload["tags"]

    def test_lora_correct_casing_ok(self):
        p = make_page(fm_yaml="type: idea\ntags:\n  - LoRA")
        assert types_of(check_lowercase_tags([p])) == []

    def test_no_tags_ok(self):
        p = make_page(fm_yaml="type: idea")
        assert types_of(check_lowercase_tags([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_inline_tags
# ────────────────────────────────────────────────────────────────────────


class TestCheckInlineTags:
    def test_block_style_ok(self):
        p = make_page(fm_yaml="type: idea\ntags:\n  - ML")
        assert types_of(check_inline_tags([p])) == []

    def test_empty_inline_ok(self):
        # tags: [] is a valid placeholder
        p = make_page(fm_yaml="type: idea\ntags: []")
        assert types_of(check_inline_tags([p])) == []

    def test_non_empty_inline_flagged(self):
        p = make_page(fm_yaml="type: idea\ntags: [ML, RL]")
        issues = issues_of(check_inline_tags([p]))
        assert len(issues) == 1
        assert issues[0].type == "inline-tags"


# ────────────────────────────────────────────────────────────────────────
# check_folder_type_mismatch
# ────────────────────────────────────────────────────────────────────────


class TestCheckFolderTypeMismatch:
    def test_matching_ok(self):
        p = make_page(folder="ideas", fm_yaml="type: idea")
        assert types_of(check_folder_type_mismatch([p])) == []

    def test_mismatch_flagged(self):
        p = make_page(folder="ideas", fm_yaml="type: entity")
        issues = issues_of(check_folder_type_mismatch([p]))
        assert len(issues) == 1
        assert issues[0].payload["expected_type"] == "idea"
        assert issues[0].payload["current_type"] == "entity"

    def test_meta_folder_exempt(self):
        p = make_page(folder="meta", fm_yaml="type: meta")
        assert types_of(check_folder_type_mismatch([p])) == []

    def test_wiki_root_exempt(self):
        p = make_page(folder="", name="index", fm_yaml="type: meta")
        assert types_of(check_folder_type_mismatch([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_raw_link_with_extension
# ────────────────────────────────────────────────────────────────────────


class TestCheckRawLinkWithExtension:
    def test_clean_link_ok(self):
        p = make_page(fm_yaml='type: idea\nsources:\n  - "[[raw/paper]]"')
        assert types_of(check_raw_link_with_extension([p])) == []

    def test_md_extension_flagged(self):
        p = make_page(fm_yaml='type: idea\nsources:\n  - "[[raw/paper.md]]"')
        issues = issues_of(check_raw_link_with_extension([p]))
        assert len(issues) == 1
        assert issues[0].type == "raw-link-with-extension"

    def test_compound_extension_ok(self):
        # raw/paper.docx.md — .md is necessary to distinguish from original
        p = make_page(fm_yaml='type: idea\nsources:\n  - "[[raw/paper.docx.md]]"')
        assert types_of(check_raw_link_with_extension([p])) == []

    def test_no_sources_ok(self):
        p = make_page(fm_yaml="type: idea")
        assert types_of(check_raw_link_with_extension([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_raw_ref_in_body
# ────────────────────────────────────────────────────────────────────────


class TestCheckRawRefInBody:
    def test_no_raw_refs_ok(self):
        p = make_page(fm_yaml="type: idea", body="See [[RLHF]] for context.\n")
        assert types_of(check_raw_ref_in_body([p])) == []

    def test_raw_ref_in_body_flagged(self):
        p = make_page(
            fm_yaml="type: idea",
            body="Based on [[raw/RLHF]] notes.\n",
        )
        issues = issues_of(check_raw_ref_in_body([p]))
        assert len(issues) == 1
        assert issues[0].type == "raw-ref-in-body"

    def test_meta_page_exempt(self):
        p = make_page(
            folder="meta",
            fm_yaml="type: meta",
            body="Ingested [[raw/paper]] at 10:00.\n",
        )
        assert types_of(check_raw_ref_in_body([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_dead_link
# ────────────────────────────────────────────────────────────────────────


class TestCheckDeadLink:
    def _two_pages(self, body_a: str = "", body_b: str = ""):
        a = make_page(name="Page-A", fm_yaml="type: idea", body=body_a)
        b = make_page(name="Page-B", fm_yaml="type: idea", body=body_b)
        return [a, b]

    def test_existing_link_ok(self):
        pages = self._two_pages(body_a="See [[Page-B]].\n")
        assert types_of(check_dead_link(pages)) == []

    def test_missing_link_flagged(self):
        pages = self._two_pages(body_a="See [[Ghost-Page]].\n")
        issues = issues_of(check_dead_link(pages))
        assert len(issues) == 1
        assert issues[0].type == "dead-link"
        assert "Ghost-Page" in issues[0].payload["what"]

    def test_link_inside_fenced_code_ignored(self):
        body = "```\nSee [[Ghost-Page]].\n```\n"
        pages = self._two_pages(body_a=body)
        assert types_of(check_dead_link(pages)) == []

    def test_link_inside_inline_code_ignored(self):
        body = "Use `[[Ghost-Page]]` syntax.\n"
        pages = self._two_pages(body_a=body)
        assert types_of(check_dead_link(pages)) == []

    def test_raw_links_ignored(self):
        pages = self._two_pages(body_a="See [[raw/paper]].\n")
        # raw links are exempt from dead-link check
        assert types_of(check_dead_link(pages)) == []

    def test_dead_link_in_frontmatter_related(self):
        p = make_page(
            fm_yaml='type: idea\nrelated:\n  - "[[NonExistent]]"',
            body="",
        )
        issues = issues_of(check_dead_link([p]))
        assert any(i.type == "dead-link" for i in issues)

    def test_meta_pages_exempt(self):
        p = make_page(
            folder="meta",
            fm_yaml="type: meta",
            body="See [[Ghost-Page]] in old report.\n",
        )
        assert types_of(check_dead_link([p])) == []

    def test_deduplicated_per_page_target(self):
        body = "[[Ghost]] appears here and [[Ghost]] again.\n"
        pages = self._two_pages(body_a=body)
        issues = issues_of(check_dead_link(pages))
        # Only one issue per (page, target) pair
        assert len(issues) == 1


# ────────────────────────────────────────────────────────────────────────
# check_orphan
# ────────────────────────────────────────────────────────────────────────


class TestCheckOrphan:
    def test_linked_page_ok(self):
        # A links to B → B has an inbound link and should NOT be orphan.
        # A itself has no inbound links, so it IS an orphan — that's correct.
        a = make_page(name="A", fm_yaml="type: idea", body="See [[B]].\n")
        b = make_page(name="B", fm_yaml="type: idea")
        orphan_pages = [i.payload["where"] for i in check_orphan([a, b])]
        assert not any("Page-B" in w or w.endswith("/B.md") for w in orphan_pages)

    def test_unlinked_page_flagged(self):
        p = make_page(name="Lonely", fm_yaml="type: idea")
        issues = issues_of(check_orphan([p]))
        assert any(i.type == "orphan" for i in issues)

    def test_meta_page_exempt(self):
        p = make_page(folder="meta", name="cache", fm_yaml="type: meta")
        assert types_of(check_orphan([p])) == []

    def test_wiki_root_file_exempt(self):
        p = make_page(folder="", name="index", fm_yaml="type: meta")
        assert types_of(check_orphan([p])) == []

    def test_frontmatter_related_counts_as_link(self):
        # A's `related:` references B — B has inbound link and must not be orphan.
        a = make_page(
            name="A",
            fm_yaml='type: idea\nrelated:\n  - "[[B]]"',
        )
        b = make_page(name="B", fm_yaml="type: idea")
        orphan_pages = [i.payload["where"] for i in check_orphan([a, b])]
        assert not any(w.endswith("/B.md") for w in orphan_pages)


# ────────────────────────────────────────────────────────────────────────
# check_asymmetric_related
# ────────────────────────────────────────────────────────────────────────


class TestCheckAsymmetricRelated:
    def test_symmetric_ok(self):
        a = make_page(name="A", fm_yaml='type: idea\nrelated:\n  - "[[B]]"')
        b = make_page(name="B", fm_yaml='type: idea\nrelated:\n  - "[[A]]"')
        assert types_of(check_asymmetric_related([a, b])) == []

    def test_asymmetric_flagged(self):
        a = make_page(name="A", fm_yaml='type: idea\nrelated:\n  - "[[B]]"')
        b = make_page(name="B", fm_yaml="type: idea\nrelated: []")
        issues = issues_of(check_asymmetric_related([a, b]))
        assert len(issues) == 1
        assert issues[0].type == "asymmetric-related"

    def test_reported_once_per_pair(self):
        # A→B and B→ nothing: only one issue, not two
        a = make_page(name="A", fm_yaml='type: idea\nrelated:\n  - "[[B]]"')
        b = make_page(name="B", fm_yaml="type: idea")
        assert len(issues_of(check_asymmetric_related([a, b]))) == 1

    def test_dead_related_link_not_double_reported(self):
        # A→Ghost: dead-link check handles this; asymmetric should skip missing pages
        a = make_page(name="A", fm_yaml='type: idea\nrelated:\n  - "[[Ghost]]"')
        issues = issues_of(check_asymmetric_related([a]))
        # Ghost doesn't exist in by_name → continue (no asymmetric issue)
        assert types_of(issues_of(check_asymmetric_related([a]))) == []


# ────────────────────────────────────────────────────────────────────────
# check_dangling_domain_ref
# ────────────────────────────────────────────────────────────────────────


class TestCheckDanglingDomainRef:
    def test_existing_domain_ok(self):
        domain = make_page(folder="domains", name="Machine-Learning", fm_yaml="type: domain")
        idea = make_page(
            fm_yaml='type: idea\ndomain:\n  - "[[Machine-Learning]]"'
        )
        assert types_of(check_dangling_domain_ref([domain, idea])) == []

    def test_missing_domain_flagged(self):
        idea = make_page(
            fm_yaml='type: idea\ndomain:\n  - "[[NonExistent-Domain]]"'
        )
        issues = issues_of(check_dangling_domain_ref([idea]))
        assert len(issues) == 1
        assert issues[0].type == "dangling-domain-ref"
        assert issues[0].payload["missing_domain"] == "NonExistent-Domain"

    def test_no_domain_field_ok(self):
        p = make_page(fm_yaml="type: idea")
        assert types_of(check_dangling_domain_ref([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_empty_section
# ────────────────────────────────────────────────────────────────────────


class TestCheckEmptySection:
    def test_section_with_content_ok(self):
        body = "## Суть\n\nHere is some content.\n"
        p = make_page(fm_yaml="type: idea", body=body)
        assert types_of(check_empty_section([p])) == []

    def test_empty_section_flagged(self):
        body = "## Суть\n\n## Контекст\n\nSome context.\n"
        p = make_page(fm_yaml="type: idea", body=body)
        issues = issues_of(check_empty_section([p]))
        assert any(i.type == "empty-section" for i in issues)
        assert any(i.payload["section"] == "Суть" for i in issues)

    def test_html_comment_not_content(self):
        # <!-- placeholder --> should NOT count as content
        body = "## Суть\n\n<!-- placeholder -->\n\n## Контекст\n\nOK\n"
        p = make_page(fm_yaml="type: idea", body=body)
        issues = issues_of(check_empty_section([p]))
        assert any(i.payload.get("section") == "Суть" for i in issues)

    def test_fenced_code_block_is_content(self):
        body = "## Суть\n\n```python\ncode here\n```\n"
        p = make_page(fm_yaml="type: idea", body=body)
        assert types_of(check_empty_section([p])) == []

    def test_meta_page_exempt(self):
        body = "## Empty\n\n## Also Empty\n"
        p = make_page(folder="meta", fm_yaml="type: meta", body=body)
        assert types_of(check_empty_section([p])) == []

    def test_nested_heading_not_empty_outer(self):
        body = "## Суть\n\n### Подраздел\n\nContent here.\n"
        p = make_page(fm_yaml="type: idea", body=body)
        # ## Суть contains content via its nested heading
        assert types_of(check_empty_section([p])) == []


# ────────────────────────────────────────────────────────────────────────
# check_stale_index_entry (filesystem-dependent)
# ────────────────────────────────────────────────────────────────────────


class TestCheckStaleIndexEntry:
    def test_valid_entry_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "WIKI_ROOT", tmp_path)
        index = tmp_path / "index.md"
        index.write_text("## Ideas\n| [[Existing-Page]] | some summary |\n")
        page = make_page(name="Existing-Page", fm_yaml="type: idea")
        assert types_of(check_stale_index_entry([page])) == []

    def test_stale_entry_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "WIKI_ROOT", tmp_path)
        index = tmp_path / "index.md"
        index.write_text("## Ideas\n| [[Deleted-Page]] | gone |\n")
        issues = issues_of(check_stale_index_entry([]))
        assert len(issues) == 1
        assert issues[0].type == "stale-index-entry"
        assert "Deleted-Page" in issues[0].payload["link"]

    def test_no_index_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "WIKI_ROOT", tmp_path)
        # No index.md — check silently returns
        assert types_of(check_stale_index_entry([])) == []


# ────────────────────────────────────────────────────────────────────────
# check_missing_index_entry (filesystem-dependent)
# ────────────────────────────────────────────────────────────────────────


class TestCheckMissingIndexEntry:
    def test_indexed_page_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "WIKI_ROOT", tmp_path)
        index = tmp_path / "index.md"
        index.write_text("## Ideas\n| [[My-Idea]] | summary |\n")
        page = make_page(name="My-Idea", fm_yaml="type: idea")
        assert types_of(check_missing_index_entry([page])) == []

    def test_missing_entry_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "WIKI_ROOT", tmp_path)
        index = tmp_path / "index.md"
        index.write_text("## Ideas\n| Страница | Суть |\n|---|---|\n")
        page = make_page(name="Unlisted-Idea", fm_yaml="type: idea")
        issues = issues_of(check_missing_index_entry([page]))
        assert len(issues) == 1
        assert issues[0].type == "missing-index-entry"

    def test_meta_pages_not_required_in_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "WIKI_ROOT", tmp_path)
        index = tmp_path / "index.md"
        index.write_text("## Ideas\n")
        meta = make_page(folder="meta", name="cache", fm_yaml="type: meta")
        assert types_of(check_missing_index_entry([meta])) == []


# ────────────────────────────────────────────────────────────────────────
# check_binary_source_outside_formats (filesystem-dependent)
# ────────────────────────────────────────────────────────────────────────


class TestCheckBinarySourceOutsideFormats:
    def test_pdf_in_raw_root_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "RAW_ROOT", tmp_path)
        monkeypatch.setattr(L, "RAW_FORMATS_DIR", tmp_path / "formats")
        (tmp_path / "paper.pdf").write_bytes(b"%PDF")
        issues = issues_of(check_binary_source_outside_formats([]))
        assert len(issues) == 1
        assert issues[0].type == "binary-source-outside-formats"

    def test_pdf_in_formats_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "RAW_ROOT", tmp_path)
        monkeypatch.setattr(L, "RAW_FORMATS_DIR", tmp_path / "formats")
        formats = tmp_path / "formats"
        formats.mkdir()
        (formats / "paper.pdf").write_bytes(b"%PDF")
        assert types_of(check_binary_source_outside_formats([])) == []

    def test_md_file_in_raw_root_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "RAW_ROOT", tmp_path)
        monkeypatch.setattr(L, "RAW_FORMATS_DIR", tmp_path / "formats")
        (tmp_path / "notes.md").write_text("# Notes")
        assert types_of(check_binary_source_outside_formats([])) == []

    def test_audio_in_raw_root_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "RAW_ROOT", tmp_path)
        monkeypatch.setattr(L, "RAW_FORMATS_DIR", tmp_path / "formats")
        (tmp_path / "lecture.mp3").write_bytes(b"")
        issues = issues_of(check_binary_source_outside_formats([]))
        assert issues[0].type == "binary-source-outside-formats"

    def test_meta_dir_exempt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "RAW_ROOT", tmp_path)
        monkeypatch.setattr(L, "RAW_FORMATS_DIR", tmp_path / "formats")
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "ingested.json").write_text("{}")
        assert types_of(check_binary_source_outside_formats([])) == []


# ────────────────────────────────────────────────────────────────────────
# Index table parser
# ────────────────────────────────────────────────────────────────────────


class TestParseIndexTables:
    def test_parses_wikilinks_from_table(self):
        text = "## Ideas\n| [[RLHF]] | RL from human feedback |\n"
        result = _parse_index_tables(text)
        assert "RLHF" in result.get("Ideas", set())

    def test_separator_row_skipped(self):
        text = "## Ideas\n| Страница | Суть |\n|---|---|\n| [[RLHF]] | desc |\n"
        result = _parse_index_tables(text)
        assert "RLHF" in result["Ideas"]
        # Separator should not appear as a name
        assert not any("---" in n for n in result["Ideas"])

    def test_unknown_section_ignored(self):
        text = "## Changelog\n| [[Foo]] | bar |\n"
        result = _parse_index_tables(text)
        assert "Changelog" not in result


# ────────────────────────────────────────────────────────────────────────
# compute_wiki_hash
# ────────────────────────────────────────────────────────────────────────


class TestComputeWikiHash:
    def test_deterministic(self):
        pages = [
            make_page(name="A", fm_yaml="type: idea", body="Content A"),
            make_page(name="B", fm_yaml="type: idea", body="Content B"),
        ]
        h1 = compute_wiki_hash(pages)
        h2 = compute_wiki_hash(pages)
        assert h1 == h2

    def test_different_content_different_hash(self):
        pages_a = [make_page(name="A", fm_yaml="type: idea", body="Content A")]
        pages_b = [make_page(name="A", fm_yaml="type: idea", body="Content B")]
        assert compute_wiki_hash(pages_a) != compute_wiki_hash(pages_b)

    def test_order_independent(self):
        a = make_page(name="A", fm_yaml="type: idea")
        b = make_page(name="B", fm_yaml="type: idea")
        assert compute_wiki_hash([a, b]) == compute_wiki_hash([b, a])

    def test_empty_returns_hash(self):
        h = compute_wiki_hash([])
        assert isinstance(h, str) and len(h) == 64


# ────────────────────────────────────────────────────────────────────────
# Layer 1.5 — embedding-based checks
# ────────────────────────────────────────────────────────────────────────


class TestWikilinkToRawKey:
    def test_simple(self):
        assert _wikilink_to_raw_key("[[raw/RLHF]]") == "RLHF.md"

    def test_subpath(self):
        assert _wikilink_to_raw_key("[[raw/articles/foo]]") == "articles/foo.md"

    def test_compound_extension_kept(self):
        assert _wikilink_to_raw_key("[[raw/paper.docx.md]]") == "paper.docx.md"

    def test_already_md(self):
        assert _wikilink_to_raw_key("[[raw/note.md]]") == "note.md"

    def test_non_raw_returns_none(self):
        assert _wikilink_to_raw_key("[[wiki/Page]]") is None

    def test_not_a_wikilink(self):
        assert _wikilink_to_raw_key("just text") is None


def _make_idx(pairs: list[tuple[str, list[float]]]) -> EmbedIndex:
    """Helper: build an in-memory EmbedIndex with given (name, vec) pairs."""
    idx = EmbedIndex(Path("/dev/null"))
    for name, vec in pairs:
        idx.upsert(name, f"content for {name}", vec)
    return idx


class TestCheckSimilarButUnlinked:
    def test_similar_unlinked_flagged(self):
        # A and B have nearly identical vectors; no link between them
        a = make_page(name="A", fm_yaml="type: idea")
        b = make_page(name="B", fm_yaml="type: idea")
        # Add a third dissimilar page so threshold makes sense
        c = make_page(name="C", fm_yaml="type: idea")
        idx = _make_idx([
            ("A", [1.0, 0.0, 0.0]),
            ("B", [0.99, 0.01, 0.0]),
            ("C", [0.0, 0.0, 1.0]),
        ])
        issues = list(check_similar_but_unlinked([a, b, c], idx, threshold_percentile=50))
        assert len(issues) == 1
        assert issues[0].type == "similar-but-unlinked"
        flagged = {issues[0].payload["page_a"], issues[0].payload["page_b"]}
        assert flagged == {a.relpath(), b.relpath()}

    def test_similar_but_linked_skipped(self):
        # A→B via body wikilink
        a = make_page(name="A", fm_yaml="type: idea", body="See [[B]].\n")
        b = make_page(name="B", fm_yaml="type: idea")
        c = make_page(name="C", fm_yaml="type: idea")
        idx = _make_idx([
            ("A", [1.0, 0.0]),
            ("B", [0.99, 0.01]),
            ("C", [0.0, 1.0]),
        ])
        issues = list(check_similar_but_unlinked([a, b, c], idx, threshold_percentile=50))
        assert len(issues) == 0

    def test_linked_via_related_skipped(self):
        a = make_page(name="A", fm_yaml='type: idea\nrelated:\n  - "[[B]]"')
        b = make_page(name="B", fm_yaml="type: idea")
        c = make_page(name="C", fm_yaml="type: idea")
        idx = _make_idx([
            ("A", [1.0, 0.0]),
            ("B", [0.99, 0.01]),
            ("C", [0.0, 1.0]),
        ])
        issues = list(check_similar_but_unlinked([a, b, c], idx, threshold_percentile=50))
        assert len(issues) == 0

    def test_dissimilar_pages_not_flagged(self):
        a = make_page(name="A", fm_yaml="type: idea")
        b = make_page(name="B", fm_yaml="type: idea")
        idx = _make_idx([
            ("A", [1.0, 0.0]),
            ("B", [0.0, 1.0]),  # orthogonal
        ])
        issues = list(check_similar_but_unlinked([a, b], idx, threshold_percentile=99))
        assert len(issues) == 0

    def test_empty_index_returns_no_issues(self):
        a = make_page(name="A", fm_yaml="type: idea")
        idx = EmbedIndex(Path("/dev/null"))
        assert list(check_similar_but_unlinked([a], idx)) == []

    def test_stale_index_entry_skipped(self):
        # Embedding for a page that no longer exists in the wiki — skip silently
        a = make_page(name="A", fm_yaml="type: idea")
        idx = _make_idx([
            ("A", [1.0, 0.0]),
            ("Deleted-Page", [0.99, 0.01]),
        ])
        issues = list(check_similar_but_unlinked([a], idx, threshold_percentile=50))
        # No pair to flag — Deleted-Page isn't in pages list
        assert len(issues) == 0

    def test_pair_reported_once(self):
        # Symmetric pair (A,B) should produce at most one issue
        a = make_page(name="A", fm_yaml="type: idea")
        b = make_page(name="B", fm_yaml="type: idea")
        idx = _make_idx([
            ("A", [1.0, 0.0]),
            ("B", [0.99, 0.01]),
        ])
        issues = list(check_similar_but_unlinked([a, b], idx, threshold_percentile=50))
        assert len(issues) <= 1


class TestCheckSynthesisDrift:
    def test_low_drift_not_flagged(self):
        # Wiki page vec ~= source vec → near-zero drift, not flagged
        page = make_page(
            name="P",
            fm_yaml='type: idea\nsources:\n  - "[[raw/source]]"',
        )
        wiki_idx = _make_idx([("P", [1.0, 0.0, 0.0])])
        raw_idx = _make_idx([("source.md", [1.0, 0.0, 0.0])])
        issues = list(check_synthesis_drift([page], wiki_idx, raw_idx))
        # All drifts are zero — nothing exceeds mean+std
        assert len(issues) == 0

    def test_high_drift_outlier_flagged(self):
        # One page drifts significantly from its source while others don't
        good_pages = [
            make_page(name=f"G{i}", fm_yaml=f'type: idea\nsources:\n  - "[[raw/s{i}]]"')
            for i in range(5)
        ]
        drifted = make_page(
            name="Drifted",
            fm_yaml='type: idea\nsources:\n  - "[[raw/sD]]"',
        )
        wiki_pairs = [(f"G{i}", [1.0, 0.0, 0.0]) for i in range(5)]
        raw_pairs = [(f"s{i}.md", [1.0, 0.0, 0.0]) for i in range(5)]
        # Drifted page is orthogonal to its source
        wiki_pairs.append(("Drifted", [0.0, 1.0, 0.0]))
        raw_pairs.append(("sD.md", [1.0, 0.0, 0.0]))
        wiki_idx = _make_idx(wiki_pairs)
        raw_idx = _make_idx(raw_pairs)
        issues = list(check_synthesis_drift(good_pages + [drifted], wiki_idx, raw_idx))
        assert any(i.payload["where"].endswith("/Drifted.md") for i in issues)

    def test_no_sources_skipped(self):
        page = make_page(name="P", fm_yaml="type: idea")
        wiki_idx = _make_idx([("P", [1.0, 0.0])])
        raw_idx = _make_idx([("any.md", [0.5, 0.5])])
        assert list(check_synthesis_drift([page], wiki_idx, raw_idx)) == []

    def test_sources_not_in_raw_index_skipped(self):
        page = make_page(
            name="P",
            fm_yaml='type: idea\nsources:\n  - "[[raw/missing]]"',
        )
        wiki_idx = _make_idx([("P", [1.0, 0.0])])
        raw_idx = _make_idx([("other.md", [0.5, 0.5])])
        # No raw vector for the source → skip
        assert list(check_synthesis_drift([page], wiki_idx, raw_idx)) == []

    def test_empty_indexes_return_no_issues(self):
        page = make_page(name="P", fm_yaml="type: idea")
        empty = EmbedIndex(Path("/dev/null"))
        assert list(check_synthesis_drift([page], empty, empty)) == []

    def test_compound_source_extension(self):
        # [[raw/paper.docx.md]] should look up "paper.docx.md" in raw_idx
        page = make_page(
            name="P",
            fm_yaml='type: idea\nsources:\n  - "[[raw/paper.docx.md]]"',
        )
        wiki_idx = _make_idx([("P", [1.0, 0.0])])
        raw_idx = _make_idx([("paper.docx.md", [1.0, 0.0])])
        # Should find the source successfully — no issue (drift is 0)
        issues = list(check_synthesis_drift([page], wiki_idx, raw_idx))
        assert len(issues) == 0

    def test_multiple_sources_aggregated(self):
        # Sources are averaged into a centroid before comparing
        page = make_page(
            name="P",
            fm_yaml='type: idea\nsources:\n  - "[[raw/s1]]"\n  - "[[raw/s2]]"',
        )
        wiki_idx = _make_idx([("P", [0.5, 0.5])])  # centroid of sources
        raw_idx = _make_idx([
            ("s1.md", [1.0, 0.0]),
            ("s2.md", [0.0, 1.0]),
        ])
        # Wiki vec equals centroid of sources → very low drift
        issues = list(check_synthesis_drift([page], wiki_idx, raw_idx))
        assert len(issues) == 0
