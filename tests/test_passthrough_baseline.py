"""Tests for ``scripts/passthrough_baseline.py`` (v0.22.0 Phase 6).

Per PIE §3, the harness MUST report 0 improvement when input == output.
We exercise the script against the Phase 0 lean fixtures and assert every
reported slim score is 0 and the exit code is 0.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "passthrough_baseline.py"
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "anti_bloat"


def test_passthrough_all_zero_scores() -> None:
    """Every lean fixture passthrough must score 0.0 and the script must
    exit 0."""
    # Precondition: lean fixtures exist (they came in with Phase 0).
    assert any(_FIXTURES.glob("pair_*.lean.py")), (
        "Phase 0 lean fixtures missing; passthrough cannot run."
    )

    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"Expected exit 0; got {proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    # Each per-pair line ends with "slim=0.000". Allow the script to log
    # via stdout or stderr (the script uses logging which goes to stderr
    # by default but may be captured in stdout if reconfigured).
    output = proc.stdout + proc.stderr
    pair_lines = [line for line in output.splitlines() if line.startswith("[OK]")]
    assert pair_lines, f"No per-pair OK lines found in output:\n{output}"
    for line in pair_lines:
        m = re.search(r"slim=(\d+\.\d+)", line)
        assert m is not None, f"Could not parse slim score from line: {line}"
        assert float(m.group(1)) == 0.0, f"Non-zero passthrough score: {line}"
    assert "harness sane" in output.lower()
