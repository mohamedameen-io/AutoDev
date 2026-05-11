"""Bug #3 regression: QA ``_iter_files`` helpers must survive 4000-char paths.

When a developer agent emits a JSON-escaped multi-line code listing into the
``diff`` field of ``.autodev/responses/{task_id}-{role}.json``, the orchestrator's
``extract_files_from_diff`` parses ``+++ b/<path>`` lines but treats the entire
blob as a single line. The extracted "path" becomes a 4000+ char string
containing literal ``\\n`` escapes.

That path flows into QA gates' ``_iter_files`` helpers. ``Path.resolve()``
succeeds (pure string), then ``resolved.is_file()`` / ``resolved.exists()``
hit ``os.stat`` which raises ``OSError: [Errno 63] File name too long``.

These tests verify the three QA helpers each gracefully skip pathological
paths instead of raising ``OSError``. Mirrors the existing ``OSError`` guard
around ``resolve()`` two lines above each call site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.code_size import _iter_python_files
from qa.hallucination_guard import _iter_files as _iter_files_hallucination
from qa.secretscan import _iter_files as _iter_files_secretscan


_PATHOLOGICAL_PATH = (
    "src/long_blob.py\n"
    + ("a" * 4000)
    + "\nmore/text/here.py"
)


def _make_pathological_paths() -> list[Path]:
    """Build the 4000-char multi-line "path" extracted from a malformed diff."""
    return [Path(_PATHOLOGICAL_PATH)]


def test_secretscan_iter_files_skips_oversized_path(tmp_path: Path) -> None:
    """``qa.secretscan._iter_files`` must not raise OSError on a 4000-char path."""
    paths = _make_pathological_paths()
    # Generator — consume eagerly so any OSError surfaces here.
    result = list(_iter_files_secretscan(tmp_path, paths=paths))
    assert result == []


def test_hallucination_guard_iter_files_skips_oversized_path(tmp_path: Path) -> None:
    """``qa.hallucination_guard._iter_files`` must not raise OSError."""
    paths = _make_pathological_paths()
    result = _iter_files_hallucination(tmp_path, paths)
    assert result == []


def test_code_size_iter_python_files_skips_oversized_path(tmp_path: Path) -> None:
    """``qa.code_size._iter_python_files`` must not raise OSError."""
    paths = _make_pathological_paths()
    result = _iter_python_files(tmp_path, paths)
    assert result == []


def test_secretscan_iter_files_keeps_valid_paths_alongside_oversized(
    tmp_path: Path,
) -> None:
    """Valid paths in the same batch must still be yielded."""
    valid = tmp_path / "real.py"
    valid.write_text("x = 1\n")
    paths = [Path(_PATHOLOGICAL_PATH), Path("real.py")]
    result = list(_iter_files_secretscan(tmp_path, paths=paths))
    assert valid.resolve() in result


def test_hallucination_guard_iter_files_keeps_valid_paths_alongside_oversized(
    tmp_path: Path,
) -> None:
    """Valid paths in the same batch must still be returned."""
    valid = tmp_path / "real.py"
    valid.write_text("x = 1\n")
    paths = [Path(_PATHOLOGICAL_PATH), Path("real.py")]
    result = _iter_files_hallucination(tmp_path, paths)
    assert valid.resolve() in result


def test_code_size_iter_python_files_keeps_valid_paths_alongside_oversized(
    tmp_path: Path,
) -> None:
    """Valid paths in the same batch must still be returned."""
    valid = tmp_path / "real.py"
    valid.write_text("x = 1\n")
    paths = [Path(_PATHOLOGICAL_PATH), Path("real.py")]
    result = _iter_python_files(tmp_path, paths)
    assert valid.resolve() in result


@pytest.mark.parametrize(
    "iter_helper",
    [
        lambda cwd, ps: list(_iter_files_secretscan(cwd, paths=ps)),
        lambda cwd, ps: _iter_files_hallucination(cwd, ps),
        lambda cwd, ps: _iter_python_files(cwd, ps),
    ],
    ids=["secretscan", "hallucination_guard", "code_size"],
)
def test_iter_files_handles_embedded_null_byte(
    tmp_path: Path, iter_helper
) -> None:
    """Embedded NUL bytes must not crash the helpers."""
    paths = [Path("src/x\x00y.py")]
    result = iter_helper(tmp_path, paths)
    assert result == []
