"""Tests for ``scripts/yap_regression.py`` (v0.22.0 Phase 6 stub).

The YapBench dataset is not yet downloaded (Phase 0 left only a README
placeholder under ``tests/benchmarks/yapbench/``). The script must:

* Detect dataset absence
* Print a clear "not present" notice
* Exit 0 (not break CI on day one)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "yap_regression.py"


def test_script_exits_zero_when_dataset_absent() -> None:
    """When tests/benchmarks/yapbench/ has no parquet/jsonl, exit 0
    with a 'not present' notice."""
    candidates = [
        _REPO_ROOT / "tests" / "benchmarks" / "yapbench" / "yapbench_dataset.parquet",
        _REPO_ROOT / "tests" / "benchmarks" / "yapbench" / "yapbench_dataset.jsonl",
    ]
    # Sanity precondition: dataset really is absent so the test is
    # exercising the right branch.
    assert not any(c.is_file() for c in candidates), (
        "Test precondition: YapBench dataset must be absent. Found: "
        f"{[c for c in candidates if c.is_file()]}"
    )

    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"Expected exit 0 when dataset absent; got {proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    # logging.basicConfig defaults to stderr — accept either stream.
    output = (proc.stdout + proc.stderr).lower()
    assert "not present" in output
