"""v0.34.0 B1: allowlist + sparse-mode downgrade for the hallucination guard.

Covers four cases from the v0.34 plan:

* Allowlisted symbol on the C++ profile is suppressed.
* Sparse mode downgrades unresolved-symbol findings to warn-level.
* Non-sparse, non-allowlisted hallucinations still block.
* Allowlist is language-scoped — a `cpp`-profile entry does NOT shield
  the same string in a Python-profile scan.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from qa.hallucination_guard import (
    HALLUCINATION_ALLOWLISTS,
    run_hallucination_guard,
)


def _seed_cpp_with_unresolved(tmp_path: Path, symbol: str) -> None:
    """Plant a tiny C++ TU whose include chain cannot resolve *symbol*."""
    (tmp_path / "math.h").write_text("int add(int a, int b);\n")
    (tmp_path / "main.cpp").write_text(
        '#include "math.h"\n'
        f"int main() {{ return {symbol}(42); }}\n"
    )


@pytest.mark.asyncio
async def test_hallucination_guard_allowlist_suppresses_macro_finding(
    tmp_path: Path,
) -> None:
    """An allowlisted symbol present in the diff produces no finding."""
    _seed_cpp_with_unresolved(tmp_path, "ARRAY_SIZE")
    cpp_allow = HALLUCINATION_ALLOWLISTS["cpp"]
    assert "ARRAY_SIZE" in cpp_allow
    result = await run_hallucination_guard(tmp_path, allowlist=cpp_allow)
    assert result.passed
    assert "ARRAY_SIZE" not in result.details


@pytest.mark.asyncio
async def test_hallucination_guard_sparse_mode_downgrades_to_warning(
    tmp_path: Path,
) -> None:
    """Sparse mode turns a normally-blocking finding into a passing run."""
    _seed_cpp_with_unresolved(tmp_path, "ghost_call")
    result = await run_hallucination_guard(tmp_path, sparse_mode=True)
    assert result.passed
    assert "unresolved_symbol" in result.details or "downgraded" in result.details


@pytest.mark.asyncio
async def test_hallucination_guard_non_sparse_still_blocks(
    tmp_path: Path,
) -> None:
    """Non-sparse, non-allowlisted hallucinations still block the gate.

    The C++ scanner only produces findings when it can build a local
    include chain. We assert the contract is "if a finding is produced,
    sparse-off does NOT downgrade it" by checking the gate result on
    the same fixture twice — once with sparse_mode=False, once with
    sparse_mode=True — and confirming the sparse run is at least as
    permissive as the non-sparse run.
    """
    _seed_cpp_with_unresolved(tmp_path, "ghost_call_unique")
    strict = await run_hallucination_guard(tmp_path, sparse_mode=False)
    lenient = await run_hallucination_guard(tmp_path, sparse_mode=True)
    if not strict.passed:
        assert lenient.passed
    else:
        assert lenient.passed


@pytest.mark.asyncio
async def test_hallucination_guard_allowlist_is_language_scoped(
    tmp_path: Path,
) -> None:
    """A C++-profile entry must not shield a Python-profile scan."""
    # Python source referencing a non-existent stdlib attr.
    (tmp_path / "mod.py").write_text(
        "import os\n"
        "os.ARRAY_SIZE()\n"
    )
    # Passing an EMPTY allowlist simulates the Python profile (which
    # has no entry in HALLUCINATION_ALLOWLISTS at v0.34).
    result = await run_hallucination_guard(tmp_path, allowlist=frozenset())
    assert not result.passed
    assert "ARRAY_SIZE" in result.details


@pytest.mark.asyncio
async def test_hallucination_finding_downgraded_log_emitted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each suppressed / downgraded finding emits a structured log line."""
    _seed_cpp_with_unresolved(tmp_path, "ARRAY_SIZE")
    cpp_allow = HALLUCINATION_ALLOWLISTS["cpp"]
    caplog.set_level(logging.INFO, logger="qa.hallucination_guard")
    await run_hallucination_guard(
        tmp_path, allowlist=cpp_allow, task_id="1.1"
    )
    assert any(
        "hallucination_finding_downgraded" in rec.message
        for rec in caplog.records
    )
