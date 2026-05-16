"""v0.34.0 integration: macro-heavy C/C++ sparse diff does not loop.

Replays the May-13-style fixture (sparse-checkout worktree edit of a
C++ TU that calls an allowlisted engine macro plus an unresolved
local symbol) through ``run_hallucination_guard`` and asserts the gate
does not block AND emits at least one
``hallucination_finding_downgraded`` log line.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from qa.hallucination_guard import (
    HALLUCINATION_ALLOWLISTS,
    run_hallucination_guard,
)


@pytest.mark.asyncio
async def test_macro_heavy_cpp_sparse_diff_does_not_loop(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sparse-worktree edit on a macro-heavy C++ TU passes the gate.

    Synthesizes a tiny TU that:
      * includes a local header (so the C++ scanner produces findings),
      * calls one allowlisted macro (`ARRAY_SIZE`) from the C++ profile,
      * calls one unresolved local symbol (`ghost_call`).

    Under sparse_mode=True with the C++ allowlist, both must drop out
    of the blocking-failure set and at least one structured log line
    must be emitted so operators can see WHY.
    """
    (tmp_path / "engine.h").write_text("void engine_init();\n")
    (tmp_path / "main.cpp").write_text(
        '#include "engine.h"\n'
        "int main() {\n"
        "    engine_init();\n"
        "    int arr[10];\n"
        "    return ARRAY_SIZE(arr) + ghost_call(0);\n"
        "}\n"
    )

    cpp_allow = HALLUCINATION_ALLOWLISTS["cpp"]

    caplog.set_level(logging.INFO, logger="qa.hallucination_guard")
    result = await run_hallucination_guard(
        tmp_path,
        allowlist=cpp_allow,
        sparse_mode=True,
        task_id="1.c9",
    )

    assert result.passed, f"expected gate to pass; got details={result.details!r}"
    # Forensic signal must be present so operators can see why we passed.
    downgrade_logs = [
        rec for rec in caplog.records
        if "hallucination_finding_downgraded" in rec.message
    ]
    assert downgrade_logs, "expected at least one downgrade log line"
