"""Tests for ``qa._io.safe_read_source``.

The helper centralizes user-source reads under the contract:

* On a clean UTF-8 file, return the decoded text.
* On a non-UTF-8 file (e.g. Latin-1 with byte ``0xe8``), substitute U+FFFD
  instead of raising :class:`UnicodeDecodeError`.
* On a missing file or non-file path, return ``None`` without raising.

The fix is wired into three scanners (``cpp_symbols.scan_cpp_file`` and
``hallucination_guard._scan_python_file`` + ``_scan_typescript_file``)
that previously raised on encountering a Latin-1 byte mid-tree (the
2026-05-11 Unity / SDL2 ``Charrière`` regression).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa._io import safe_read_source


def test_safe_read_source_utf8_clean(tmp_path: Path) -> None:
    target = tmp_path / "hello.cpp"
    target.write_text("int main() { return 0; }\n", encoding="utf-8")

    out = safe_read_source(target)

    assert out is not None
    assert "int main()" in out


def test_safe_read_source_latin1_fallback(tmp_path: Path) -> None:
    target = tmp_path / "latin1.cpp"
    # Mimic SDL2's header: an "è" byte (0xe8) buried in an ASCII-dominant
    # comment. read_text(encoding="utf-8") would raise UnicodeDecodeError;
    # safe_read_source must substitute U+FFFD instead.
    raw = b"// Luc-Olivier de Charri\xe8re\nint main(){return 0;}\n"
    target.write_bytes(raw)

    out = safe_read_source(target)

    assert out is not None
    assert "�" in out  # the replacement character
    assert "int main()" in out


def test_safe_read_source_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.cpp"

    out = safe_read_source(missing)

    assert out is None


def test_safe_read_source_directory_path(tmp_path: Path) -> None:
    # Pointing the helper at a directory should not raise — it should
    # return None just like a missing file. Covers the ValueError /
    # IsADirectoryError edge.
    out = safe_read_source(tmp_path)

    assert out is None


@pytest.mark.parametrize(
    "ext,content",
    [
        (".cpp", b'#include "foo.h"\n// Charri\xe8re\nint x = 1;\n'),
        (".py", b"# Charri\xe8re\nimport os\nx = os.path.join('a', 'b')\n"),
        (".ts", b'// Charri\xe8re\nimport x from "react";\n'),
    ],
)
def test_safe_read_source_handles_latin1_in_multiple_extensions(
    tmp_path: Path, ext: str, content: bytes
) -> None:
    target = tmp_path / f"sample{ext}"
    target.write_bytes(content)

    out = safe_read_source(target)

    assert out is not None
    assert "�" in out
