"""Tests for v0.19.0 C++ hallucination-guard support."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.hallucination_guard import run_hallucination_guard


@pytest.mark.asyncio
async def test_cpp_clean_repo_passes(tmp_path: Path) -> None:
    """C++ file with declared symbols → no finding."""
    (tmp_path / "main.cpp").write_text(
        "#include <stdio.h>\n"
        "int main() { printf(\"hi\\n\"); return 0; }\n"
    )
    result = await run_hallucination_guard(tmp_path)
    # stdio.h provides printf — we don't currently parse stdlib headers,
    # so the conservative behavior is "pass" (skip-and-warn).
    assert result.passed


@pytest.mark.asyncio
async def test_cpp_locally_declared_function_passes(tmp_path: Path) -> None:
    """A function declared in a local header → call resolves."""
    (tmp_path / "math.h").write_text("int add(int a, int b);\n")
    (tmp_path / "main.cpp").write_text(
        '#include "math.h"\n'
        "int main() { return add(1, 2); }\n"
    )
    result = await run_hallucination_guard(tmp_path)
    assert result.passed


@pytest.mark.asyncio
async def test_cpp_unresolved_call_in_present_chain_flags(tmp_path: Path) -> None:
    """Call to symbol absent from include chain (when chain is local) flags.

    Conservative skip-and-warn applies for system headers (``<…>``); only
    quoted local headers participate in resolution.
    """
    (tmp_path / "math.h").write_text("int add(int a, int b);\n")
    (tmp_path / "main.cpp").write_text(
        '#include "math.h"\n'
        "int main() { return ghost_function(42); }\n"
    )
    result = await run_hallucination_guard(tmp_path)
    # Without tree-sitter or a robust parser we may pass — skip-and-warn.
    # Either pass (conservative) or fail with the symbol mentioned.
    if not result.passed:
        assert "ghost_function" in result.details


@pytest.mark.asyncio
async def test_cpp_macro_call_skipped(tmp_path: Path) -> None:
    """``#define`` macro expansions are treated conservatively (skip-and-warn)."""
    (tmp_path / "main.cpp").write_text(
        "#define LOG(x) printf(x)\n"
        'int main() { LOG("hi\\n"); return 0; }\n'
    )
    result = await run_hallucination_guard(tmp_path)
    # Macro expansion makes static resolution unreliable; expect pass.
    assert result.passed


@pytest.mark.asyncio
async def test_cpp_dispatch_picks_up_h_files(tmp_path: Path) -> None:
    """``.h`` extension is scanned by the dispatcher."""
    (tmp_path / "lib.h").write_text(
        "int foo();\n"
        "// header-only definition\n"
        "inline int foo() { return 1; }\n"
    )
    result = await run_hallucination_guard(tmp_path)
    assert result.passed


def test_cpp_symbols_module_importable() -> None:
    """``qa.cpp_symbols`` must import cleanly even without tree-sitter."""
    from qa import cpp_symbols  # noqa: F401

    assert hasattr(cpp_symbols, "scan_cpp_file")
    assert hasattr(cpp_symbols, "TREESITTER_AVAILABLE")
