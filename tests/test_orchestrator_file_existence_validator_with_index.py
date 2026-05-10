"""Tests for v0.25.0 ``orchestrator.file_existence_validator`` index-aware
fuzzy-suggestion path.

The v0.24.3 ``_RepoFileSnapshot.closest()`` used difflib over a cached
``git ls-files`` snapshot. v0.25.0 adds an index-first preference: when
``.autodev/index.db`` exists and ``IndexQuery`` is importable, ``closest``
asks ``IndexQuery.search_files(rel_path, limit=1)`` first and falls back
to difflib only on a miss / index error.

Two behaviors covered (per the v0.25.0 plan):

  * ``test_closest_prefers_index_over_git_lsfiles_when_available`` — when
    an IndexQuery returns a hit, ``closest`` returns its ``.path`` (NOT a
    difflib match).
  * ``test_closest_falls_back_to_git_when_index_missing`` — when no
    IndexQuery is supplied (or the db doesn't exist), the v0.24.3 difflib
    path runs unchanged.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from orchestrator.file_existence_validator import _RepoFileSnapshot


def _git_init(repo: Path) -> None:
    """Bootstrap a git repo with one tracked file."""
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )
    (repo / "src").mkdir()
    (repo / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(repo), check=True
    )


@dataclass
class _StubFileHit:
    """Stand-in for ``state.file_index.FileHit``."""
    path: str
    lang: str = "cpp"


def test_closest_prefers_index_over_git_lsfiles_when_available(
    tmp_path: Path,
) -> None:
    """When ``IndexQuery.search_files`` returns a hit, ``closest()`` returns
    its ``.path`` immediately (no difflib computation).

    We pre-create a real git repo with ``src/foo.cpp`` so the difflib
    fallback would also match — but we wire a fake IndexQuery that returns
    a DIFFERENT path (``src/index_picked.cpp``) and verify the index path
    wins. This proves the index-first preference, not just "either path
    happens to find the file".
    """
    _git_init(tmp_path)

    fake_query = mock.MagicMock()
    fake_query.search_files.return_value = [
        _StubFileHit(path="src/index_picked.cpp")
    ]

    snapshot = _RepoFileSnapshot.for_cwd(tmp_path, index_query=fake_query)
    result = snapshot.closest("src/foo.cp")

    assert result == "src/index_picked.cpp"
    fake_query.search_files.assert_called_once_with("src/foo.cp", limit=1)


def test_closest_falls_back_to_git_when_index_missing(tmp_path: Path) -> None:
    """No IndexQuery supplied → the v0.24.3 difflib-over-git-ls-files path
    runs unchanged. The typo ``src/foo.cp`` finds ``src/foo.cpp``."""
    _git_init(tmp_path)

    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)  # no index_query
    result = snapshot.closest("src/foo.cp")

    assert result == "src/foo.cpp"
