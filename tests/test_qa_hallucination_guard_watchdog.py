"""v0.22.1 A1 regression: hallucination_guard per-file watchdog.

A misbehaving regex (or pathologically large file) used to pin the
orchestrator's main thread. The watchdog now wraps each file scan in
``asyncio.wait_for(asyncio.to_thread(...))`` and skip-and-warns on
timeout.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from qa import hallucination_guard
from qa.hallucination_guard import _dispatch_with_timeout, run_hallucination_guard


@pytest.mark.asyncio
async def test_dispatch_with_timeout_returns_findings_on_clean_run(tmp_path: Path) -> None:
    """Normal scan completes inside the timeout; findings are forwarded."""
    f = tmp_path / "foo.py"
    f.write_text("import os\nprint(os.path.exists('/tmp'))\n")
    out = await _dispatch_with_timeout(f, repo_root=tmp_path, timeout_s=5.0)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_dispatch_with_timeout_skips_on_runaway(tmp_path: Path) -> None:
    """A scan that exceeds timeout returns empty findings (skip-and-warn)."""

    def slow_dispatch(path: Path, repo_root: Path) -> list[str]:
        time.sleep(2.0)
        return ["should-not-appear"]

    f = tmp_path / "bad.py"
    f.write_text("x = 1\n")

    with patch.object(hallucination_guard, "_dispatch", slow_dispatch):
        start = time.time()
        out = await _dispatch_with_timeout(f, repo_root=tmp_path, timeout_s=0.3)
        elapsed = time.time() - start
    assert out == []
    # Watchdog fired well before slow_dispatch's 2s sleep.
    assert elapsed < 1.5, f"watchdog fire took {elapsed:.2f}s — should be ~0.3s"


@pytest.mark.asyncio
async def test_run_hallucination_guard_survives_runaway_file(tmp_path: Path) -> None:
    """A single bad file does not block the rest of the scan."""

    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("x = 1\n")

    real_dispatch = hallucination_guard._dispatch

    def maybe_slow(path: Path, repo_root: Path) -> list[str]:
        if path.name == "bad.py":
            time.sleep(2.0)
            return ["fake-finding"]
        return real_dispatch(path, repo_root)

    with patch.object(hallucination_guard, "_dispatch", maybe_slow):
        result = await run_hallucination_guard(
            tmp_path,
            paths=[tmp_path / "ok.py", tmp_path / "bad.py"],
            per_file_timeout_s=0.3,
        )

    # The bad file's "finding" was suppressed by the watchdog.
    assert "fake-finding" not in (result.details or "")
