"""Debug-tag cleanup gate (ADR-0046, Phase 6).

Asserts :func:`qa.debug_tag_gate.run_debug_tag_gate`:

* BLOCKS (passed=False, severity="block") when a changed file still contains a
  leftover ``[DEBUG-XYZ]`` marker.
* PASSES on a clean file with no markers.
* Respects diff-scope (``paths``) and the empty-scope no-op.
* Honors a configurable custom pattern.
* Reuses the secret-scan machinery's oversized-path resilience (never raises).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.debug_tag_gate import run_debug_tag_gate


@pytest.mark.asyncio
async def test_blocks_on_leftover_debug_tag(tmp_path: Path) -> None:
    """A file containing ``[DEBUG-XYZ]`` blocks the gate."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text(
        "def handler():\n"
        '    print("[DEBUG-AUTH] entering handler")\n'
        "    return 1\n"
    )

    result = await run_debug_tag_gate(tmp_path, paths=[Path("src/mod.py")])

    assert result.passed is False
    assert result.severity == "block"
    assert "[DEBUG-AUTH]" in result.details
    assert "src/mod.py" in result.details
    assert result.metrics["debug_tags_found"] == 1
    assert result.metrics["files_with_debug_tags"] == 1


@pytest.mark.asyncio
async def test_passes_clean_file(tmp_path: Path) -> None:
    """A file with no markers passes the gate."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text("def handler():\n    return 1\n")

    result = await run_debug_tag_gate(tmp_path, paths=[Path("src/mod.py")])

    assert result.passed is True
    assert result.metrics["debug_tags_found"] == 0


@pytest.mark.asyncio
async def test_word_debug_alone_does_not_match(tmp_path: Path) -> None:
    """Prose mentioning 'debug' (no ``[DEBUG-`` marker) does not trip the gate."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text(
        '# this function helps debug the auth flow\n'
        'logger.debug("normal log line")\n'
    )

    result = await run_debug_tag_gate(tmp_path, paths=[Path("src/mod.py")])

    assert result.passed is True


@pytest.mark.asyncio
async def test_multiple_tags_counted(tmp_path: Path) -> None:
    """Multiple markers across lines are all reported and counted."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text(
        'print("[DEBUG-1] a")\n'
        'print("[debug-trace] b")\n'  # case-insensitive
        'x = 3\n'
        'print("[DEBUG-AUTH-FLOW] c")\n'
    )

    result = await run_debug_tag_gate(tmp_path, paths=[Path("src/mod.py")])

    assert result.passed is False
    assert result.metrics["debug_tags_found"] == 3
    assert result.metrics["files_with_debug_tags"] == 1


@pytest.mark.asyncio
async def test_empty_scope_is_noop(tmp_path: Path) -> None:
    """An empty ``paths`` list short-circuits to a clean info pass."""
    result = await run_debug_tag_gate(tmp_path, paths=[])

    assert result.passed is True
    assert result.severity == "info"
    assert "no files in diff scope" in result.details


@pytest.mark.asyncio
async def test_only_scans_listed_paths(tmp_path: Path) -> None:
    """A tagged file NOT in the diff-scope list is not scanned."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "changed.py").write_text("clean = True\n")
    (src / "untouched.py").write_text('print("[DEBUG-X] leftover")\n')

    result = await run_debug_tag_gate(tmp_path, paths=[Path("src/changed.py")])

    assert result.passed is True


@pytest.mark.asyncio
async def test_custom_pattern(tmp_path: Path) -> None:
    """A configurable pattern overrides the default marker family."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text('print("XXXTODO remove me")\nx = 1\n')

    # Default pattern: no match.
    default = await run_debug_tag_gate(tmp_path, paths=[Path("src/mod.py")])
    assert default.passed is True

    # Custom pattern: matches.
    custom = await run_debug_tag_gate(
        tmp_path, paths=[Path("src/mod.py")], pattern=r"XXXTODO"
    )
    assert custom.passed is False
    assert custom.metrics["debug_tags_found"] == 1


@pytest.mark.asyncio
async def test_oversized_path_does_not_crash(tmp_path: Path) -> None:
    """A 4000-char pathological path (malformed diff) is skipped, not raised."""
    pathological = Path("src/x.py\n" + ("a" * 4000) + "\nmore.py")
    # Must not raise OSError; resolves to a clean pass (nothing scannable).
    result = await run_debug_tag_gate(tmp_path, paths=[pathological])
    assert result.passed is True


@pytest.mark.asyncio
async def test_whole_tree_walk_when_paths_none(tmp_path: Path) -> None:
    """``paths=None`` walks the whole tree (legacy mode) and finds tags."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text('print("[DEBUG-LEFTOVER] x")\n')

    result = await run_debug_tag_gate(tmp_path, paths=None)

    assert result.passed is False
    assert "[DEBUG-LEFTOVER]" in result.details
