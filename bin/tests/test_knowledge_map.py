"""Unit tests for bin/knowledge_map.py.

Covers data preparation, color blending, edge construction, statistics,
and artifact-page rendering. The viz parts (Plotly, UMAP) are not unit-
tested — they depend on optional packages and randomness; smoke-test via
live run instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_map import (
    PageInfo,
    _is_content_page,
    assign_domain_colors,
    blend_domain_colors,
    build_dataset,
    build_edges,
    collect_domains,
    compute_statistics,
    hex_to_rgb,
    render_artifact_page,
    rgb_to_hex,
)
from embed import EmbedIndex
from lint import parse_frontmatter, Page


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def make_page(
    name: str = "Test",
    folder: str = "ideas",
    fm_yaml: str = "",
    body: str = "",
) -> Page:
    text = f"---\n{fm_yaml}\n---\n{body}" if fm_yaml else body
    fm, parsed_body = parse_frontmatter(text)
    path = (
        Path(f"wiki/{folder}/{name}.md") if folder else Path(f"wiki/{name}.md")
    )
    return Page(path=path, folder=folder, name=name, text=text, fm=fm, body=parsed_body)


def make_idx(pairs: list[tuple[str, list[float]]]) -> EmbedIndex:
    idx = EmbedIndex(Path("/dev/null/missing"))
    for name, vec in pairs:
        idx.upsert(name, f"content for {name}", vec)
    return idx


# ────────────────────────────────────────────────────────────────────────
# _is_content_page
# ────────────────────────────────────────────────────────────────────────


class TestIsContentPage:
    def test_idea_is_content(self):
        p = make_page(folder="ideas", fm_yaml="type: idea")
        assert _is_content_page(p) is True

    def test_meta_excluded_by_type(self):
        p = make_page(folder="meta", fm_yaml="type: meta")
        assert _is_content_page(p) is False

    def test_root_file_excluded(self):
        p = make_page(folder="", name="cache", fm_yaml="type: meta")
        assert _is_content_page(p) is False

    def test_meta_folder_excluded_even_without_type(self):
        p = make_page(folder="meta", fm_yaml="")
        assert _is_content_page(p) is False


# ────────────────────────────────────────────────────────────────────────
# Color helpers
# ────────────────────────────────────────────────────────────────────────


class TestColorHelpers:
    def test_hex_to_rgb_basic(self):
        assert hex_to_rgb("#ff0000") == (255, 0, 0)
        assert hex_to_rgb("00ff00") == (0, 255, 0)
        assert hex_to_rgb("#0000ff") == (0, 0, 255)

    def test_hex_to_rgb_uppercase(self):
        assert hex_to_rgb("#FFFFFF") == (255, 255, 255)

    def test_rgb_to_hex_roundtrip(self):
        for h in ["#000000", "#ff8800", "#abcdef"]:
            assert rgb_to_hex(hex_to_rgb(h)) == h

    def test_assign_domain_colors_stable(self):
        # Same palette + same domains → same assignment, regardless of input order
        a = assign_domain_colors(["RL", "ML"], ["#aaa", "#bbb"])
        b = assign_domain_colors(["ML", "RL"], ["#aaa", "#bbb"])
        assert a == b

    def test_assign_domain_colors_cycles_palette(self):
        out = assign_domain_colors(
            ["A", "B", "C", "D"], ["#aaa", "#bbb"],
        )
        # 4 domains with 2-color palette → wrap around
        assert len(set(out.values())) == 2

    def test_blend_single_domain(self):
        out = blend_domain_colors(["X"], {"X": "#ff0000"})
        assert out == "#ff0000"

    def test_blend_two_domains_averages(self):
        # red + blue → purple-ish
        out = blend_domain_colors(["X", "Y"], {"X": "#ff0000", "Y": "#0000ff"})
        # Average: (127, 0, 127) → '#7f007f' (integer division)
        assert out == "#7f007f"

    def test_blend_no_domains_returns_default(self):
        assert blend_domain_colors([], {}) == "#cccccc"

    def test_blend_unknown_domain_returns_default(self):
        # Domain string not in color map
        assert blend_domain_colors(["Unknown"], {"Other": "#fff"}) == "#cccccc"

    def test_blend_custom_default(self):
        assert blend_domain_colors([], {}, default="#000") == "#000"


# ────────────────────────────────────────────────────────────────────────
# build_dataset
# ────────────────────────────────────────────────────────────────────────


class TestBuildDataset:
    def test_excludes_meta_pages(self):
        meta = make_page(folder="meta", name="cache", fm_yaml="type: meta")
        idea = make_page(name="A", fm_yaml="type: idea")
        infos = build_dataset([meta, idea], make_idx([]))
        names = [info.name for info in infos]
        assert "cache" not in names
        assert "A" in names

    def test_extracts_domains_from_frontmatter(self):
        p = make_page(
            name="A",
            fm_yaml='type: idea\ndomain:\n  - "[[ML]]"\n  - "[[RL]]"',
        )
        infos = build_dataset([p], make_idx([]))
        assert sorted(infos[0].domains) == ["ML", "RL"]

    def test_normalizes_path_prefixed_domain(self):
        # [[wiki/domains/ML]] should normalize to "ML"
        p = make_page(
            name="A",
            fm_yaml='type: idea\ndomain:\n  - "[[wiki/domains/ML]]"',
        )
        infos = build_dataset([p], make_idx([]))
        assert infos[0].domains == ["ML"]

    def test_attaches_vec_when_present(self):
        p = make_page(name="A", fm_yaml="type: idea")
        idx = make_idx([("A", [0.1, 0.2])])
        infos = build_dataset([p], idx)
        assert infos[0].vec == [0.1, 0.2]

    def test_vec_none_when_absent(self):
        p = make_page(name="A", fm_yaml="type: idea")
        infos = build_dataset([p], make_idx([]))
        assert infos[0].vec is None

    def test_extracts_body_links(self):
        p = make_page(name="A", fm_yaml="type: idea", body="See [[B]] and [[C]].\n")
        infos = build_dataset([p], make_idx([]))
        assert infos[0].body_links == {"B", "C"}

    def test_excludes_raw_links_from_body(self):
        p = make_page(
            name="A", fm_yaml="type: idea",
            body="See [[B]] and [[raw/X]].\n",
        )
        infos = build_dataset([p], make_idx([]))
        assert infos[0].body_links == {"B"}

    def test_extracts_fm_related(self):
        p = make_page(
            name="A",
            fm_yaml='type: idea\nrelated:\n  - "[[B]]"\n  - "[[C]]"',
        )
        infos = build_dataset([p], make_idx([]))
        assert infos[0].fm_links == {"B", "C"}

    def test_inbound_computed_from_outbound(self):
        a = make_page(name="A", fm_yaml="type: idea", body="See [[B]].\n")
        b = make_page(name="B", fm_yaml="type: idea")
        infos = build_dataset([a, b], make_idx([]))
        b_info = next(i for i in infos if i.name == "B")
        assert b_info.inbound == {"A"}

    def test_inbound_counts_fm_related_too(self):
        a = make_page(name="A", fm_yaml='type: idea\nrelated:\n  - "[[B]]"')
        b = make_page(name="B", fm_yaml="type: idea")
        infos = build_dataset([a, b], make_idx([]))
        b_info = next(i for i in infos if i.name == "B")
        assert "A" in b_info.inbound


# ────────────────────────────────────────────────────────────────────────
# collect_domains / build_edges
# ────────────────────────────────────────────────────────────────────────


class TestCollectDomains:
    def test_counts_pages_per_domain(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", ["ML"], None),
            PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", ["ML", "RL"], None),
            PageInfo("C", "wiki/ideas/C.md", "idea", "ideas", [], None),
        ]
        counts = collect_domains(infos)
        assert counts == {"ML": 2, "RL": 1}


class TestBuildEdges:
    def test_dedup_undirected(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None,
                     body_links={"B"}, fm_links=set()),
            PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None,
                     body_links={"A"}, fm_links=set()),
        ]
        edges = build_edges(infos)
        assert edges == [(0, 1)]

    def test_one_directional_link(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None,
                     body_links={"B"}, fm_links=set()),
            PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None,
                     body_links=set(), fm_links=set()),
        ]
        edges = build_edges(infos)
        assert edges == [(0, 1)]

    def test_self_loops_dropped(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None,
                     body_links={"A"}, fm_links=set()),
        ]
        assert build_edges(infos) == []

    def test_links_to_non_content_dropped(self):
        # Link to "Ghost" which isn't in the dataset → no edge
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None,
                     body_links={"Ghost"}, fm_links=set()),
        ]
        assert build_edges(infos) == []

    def test_body_and_fm_links_combined(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None,
                     body_links={"B"}, fm_links={"C"}),
            PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None,
                     body_links=set(), fm_links=set()),
            PageInfo("C", "wiki/ideas/C.md", "idea", "ideas", [], None,
                     body_links=set(), fm_links=set()),
        ]
        assert sorted(build_edges(infos)) == [(0, 1), (0, 2)]


# ────────────────────────────────────────────────────────────────────────
# compute_statistics
# ────────────────────────────────────────────────────────────────────────


class TestComputeStatistics:
    def test_type_counts(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None),
            PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None),
            PageInfo("E", "wiki/entities/E.md", "entity", "entities", [], None),
        ]
        stats = compute_statistics(infos)
        assert stats["type_counts"] == {"idea": 2, "entity": 1}

    def test_unassigned_counted(self):
        infos = [
            PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", ["ML"], None),
            PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None),
        ]
        stats = compute_statistics(infos)
        assert stats["unassigned"] == 1

    def test_orphans_listed(self):
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None)
        b = PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None)
        c = PageInfo("C", "wiki/ideas/C.md", "idea", "ideas", [], None,
                     body_links={"A"})
        # After build_dataset would set inbound, but we're testing
        # compute_statistics in isolation. Manually set:
        a.inbound = {"C"}
        # B and C have no inbound
        stats = compute_statistics([a, b, c])
        assert "B" in stats["orphans"]
        assert "C" in stats["orphans"]
        assert "A" not in stats["orphans"]

    def test_most_connected_finds_max_inbound(self):
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None)
        a.inbound = {"X", "Y", "Z"}
        b = PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None)
        b.inbound = {"X"}
        stats = compute_statistics([a, b])
        names = [p["name"] for p in stats["most_connected"]]
        assert "A" in names
        assert "B" not in names

    def test_no_inbound_no_most_connected(self):
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None)
        b = PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], None)
        stats = compute_statistics([a, b])
        assert stats["most_connected"] == []

    def test_tightest_pair_identified(self):
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], [1.0, 0.0])
        b = PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], [0.99, 0.01])
        c = PageInfo("C", "wiki/ideas/C.md", "idea", "ideas", [], [0.0, 1.0])
        stats = compute_statistics([a, b, c])
        assert stats["tightest_pair"] is not None
        a_, b_, _ = stats["tightest_pair"]
        assert {a_, b_} == {"A", "B"}

    def test_most_isolated_minimizes_max_sim(self):
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], [1.0, 0.0])
        b = PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", [], [0.99, 0.01])
        c = PageInfo("C", "wiki/ideas/C.md", "idea", "ideas", [], [0.0, 1.0])
        stats = compute_statistics([a, b, c])
        # C is orthogonal to both A and B → most isolated
        n, _ = stats["most_isolated"]
        assert n == "C"

    def test_domain_avg_cosine_per_domain(self):
        # ML domain: A and B are very similar
        # RL domain: C alone (skipped — need ≥2 members)
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", ["ML"], [1.0, 0.0])
        b = PageInfo("B", "wiki/ideas/B.md", "idea", "ideas", ["ML"], [0.99, 0.01])
        c = PageInfo("C", "wiki/ideas/C.md", "idea", "ideas", ["RL"], [0.0, 1.0])
        stats = compute_statistics([a, b, c])
        assert "ML" in stats["domain_avg_cosine"]
        assert stats["domain_avg_cosine"]["ML"] > 0.9
        assert "RL" not in stats["domain_avg_cosine"]  # only 1 member

    def test_no_vecs_no_semantic_stats(self):
        a = PageInfo("A", "wiki/ideas/A.md", "idea", "ideas", [], None)
        stats = compute_statistics([a])
        assert stats["sim_count"] == 0
        assert stats["tightest_pair"] is None


# ────────────────────────────────────────────────────────────────────────
# render_artifact_page
# ────────────────────────────────────────────────────────────────────────


class TestRenderArtifactPage:
    def _stub_stats(self):
        return {
            "type_counts": {"idea": 5, "entity": 2},
            "domain_counts": {"ML": 3, "RL": 2},
            "unassigned": 2,
            "valid_outlinks": 12,
            "orphans": ["X"],
            "most_connected": [{"name": "RLHF", "inbound": 4}],
            "sim_count": 21,
            "sim_median": 0.45,
            "sim_p75": 0.60,
            "sim_p95": 0.78,
            "sim_max": 0.85,
            "tightest_pair": ("RLHF", "PPO", 0.85),
            "most_isolated": ("Lonely", 0.21),
            "domain_avg_cosine": {"ML": 0.55, "RL": 0.62},
        }

    def test_frontmatter_present(self):
        page = render_artifact_page(
            self._stub_stats(), "kmap.png", "kmap.html", "2026-05-01T10:00:00",
        )
        assert page.startswith("---\n")
        assert "type: meta" in page
        assert "generated: 2026-05-01T10:00:00" in page
        assert "pages_total: 7" in page

    def test_image_embed_present(self):
        page = render_artifact_page(
            self._stub_stats(), "kmap.png", "kmap.html", "2026-05-01T10:00:00",
        )
        assert "![[kmap.png]]" in page

    def test_html_path_referenced(self):
        page = render_artifact_page(
            self._stub_stats(), "kmap.png", "kmap.html", "2026-05-01T10:00:00",
        )
        assert "kmap.html" in page

    def test_all_sections_present(self):
        page = render_artifact_page(
            self._stub_stats(), "kmap.png", "kmap.html", "2026-05-01T10:00:00",
        )
        for section in ("## Counts", "## Connectivity",
                        "## Semantic structure", "## Domain coverage"):
            assert section in page

    def test_tightest_pair_rendered(self):
        page = render_artifact_page(
            self._stub_stats(), "kmap.png", "kmap.html", "2026-05-01T10:00:00",
        )
        assert "[[RLHF]]" in page
        assert "[[PPO]]" in page
        assert "0.850" in page

    def test_orphan_count_rendered(self):
        page = render_artifact_page(
            self._stub_stats(), "kmap.png", "kmap.html", "2026-05-01T10:00:00",
        )
        assert "Orphan pages: 1" in page

    def test_no_domain_coverage_when_empty(self):
        stats = self._stub_stats()
        stats["domain_avg_cosine"] = {}
        page = render_artifact_page(stats, "k.png", "k.html", "2026-05-01T10:00:00")
        assert "## Domain coverage" not in page

    def test_irregular_plurals(self):
        stats = self._stub_stats()
        stats["type_counts"] = {"idea": 5, "entity": 3, "question": 2, "domain": 1}
        page = render_artifact_page(stats, "k.png", "k.html", "2026-05-01T10:00:00")
        assert "5 ideas" in page
        assert "3 entities" in page  # not "entitys"!
        assert "2 questions" in page
        assert "1 domain" in page  # singular when count is 1
