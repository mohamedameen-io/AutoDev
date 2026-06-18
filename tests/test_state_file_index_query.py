"""Tests for :class:`state.file_index.IndexQuery` (v0.25.0)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from state.file_index import (
    CandidateDigest,
    IndexBuilder,
    IndexQuery,
    SymbolHit,
    _tokenize_spec,
)
from state.paths import index_db_path


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )


def _git_commit(repo: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(repo), check=True
    )


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "src").mkdir()
    (repo / "src" / "parse_plan.py").write_text(
        "def parse_plan_markdown(md: str) -> dict:\n"
        "    return {}\n"
        "\n"
        "class PlanParseError(Exception):\n"
        "    pass\n"
    )
    (repo / "src" / "validate_files.py").write_text(
        "def validate_files_exist(plan, cwd):\n"
        "    return None\n"
    )
    (repo / "src" / "common.py").write_text(
        "def helper():\n"
        "    return 1\n"
        "\n"
        "MAX_RETRIES = 3\n"
    )
    _git_commit(repo)
    db = index_db_path(repo)
    IndexBuilder.build_full(repo, db)
    return repo, db


def test_search_symbols_exact_match(tmp_path: Path) -> None:
    _, db = _build_fixture(tmp_path)
    q = IndexQuery(db)
    try:
        hits = q.search_symbols("parse_plan_markdown")
        assert len(hits) >= 1
        assert hits[0].name == "parse_plan_markdown"
        assert hits[0].file_path.endswith("parse_plan.py")
        assert hits[0].kind == "function"
    finally:
        q.close()


def test_search_symbols_fts_prefix_match(tmp_path: Path) -> None:
    """FTS5 prefix MATCH should hit on 'parse' for 'parse_plan_markdown'."""
    _, db = _build_fixture(tmp_path)
    q = IndexQuery(db)
    try:
        hits = q.search_symbols("parse")
        names = {h.name for h in hits}
        assert "parse_plan_markdown" in names
    finally:
        q.close()


def test_search_files_substring(tmp_path: Path) -> None:
    _, db = _build_fixture(tmp_path)
    q = IndexQuery(db)
    try:
        hits = q.search_files("parse_plan")
        paths = {h.path for h in hits}
        assert any(p.endswith("parse_plan.py") for p in paths)
    finally:
        q.close()


def test_get_candidates_for_spec_extracts_identifiers_from_camel_and_snake_case(
    tmp_path: Path,
) -> None:
    """Spec text 'refactor parsePlanMarkdown' hits parse_plan.py."""
    # Verify the underlying tokenizer first.
    tokens = _tokenize_spec("refactor parsePlanMarkdown handler")
    # snake split + camelCase split + original camel form should all
    # appear (subset check; order-insensitive).
    lc = {t.lower() for t in tokens}
    assert "parse" in lc
    assert "plan" in lc
    assert "markdown" in lc
    assert "parseplanmarkdown" in lc

    _, db = _build_fixture(tmp_path)
    q = IndexQuery(db)
    try:
        digest = q.get_candidates_for_spec(
            "refactor parsePlanMarkdown to use new validateFilesExist helper"
        )
        names = {h.name for h in digest.symbol_hits}
        # The snake_case form lives in the index; the spec said camel.
        assert (
            "parse_plan_markdown" in names
            or "validate_files_exist" in names
        )
    finally:
        q.close()


def test_candidate_digest_respects_max_chars(tmp_path: Path) -> None:
    """Render is bounded to ``max_chars`` (or close — last-resort hard cap)."""
    big_symbols = [
        SymbolHit(
            name=f"sym_{i}",
            kind="function",
            file_path=f"src/m_{i}.py",
            line=i + 1,
            signature=f"def sym_{i}(): pass",
        )
        for i in range(50)
    ]
    digest = CandidateDigest(symbol_hits=big_symbols)
    rendered = digest.render(max_chars=400)
    assert len(rendered) <= 500  # tolerance for hard-cap suffix


def test_candidate_digest_truncation_flag(tmp_path: Path) -> None:
    """Renderer marks output truncated when items dropped or hard-capped."""
    big_symbols = [
        SymbolHit(
            name=f"verylongsymbolname_{i}",
            kind="function",
            file_path=f"src/very_long_path_to_module_{i}.py",
            line=i + 1,
            signature=f"def verylongsymbolname_{i}(): pass",
        )
        for i in range(30)
    ]
    digest = CandidateDigest(symbol_hits=big_symbols)
    rendered = digest.render(max_chars=400)
    assert "(truncated" in rendered


def test_meta_summary_returns_recorded_keys(tmp_path: Path) -> None:
    _, db = _build_fixture(tmp_path)
    q = IndexQuery(db)
    try:
        meta = q.meta_summary()
        assert meta.get("index_version") == "1"
        assert "file_count" in meta
        assert "symbol_count" in meta
    finally:
        q.close()
