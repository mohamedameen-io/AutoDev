"""Per-repo secretscan baseline (v0.19.0).

Some repos contain pre-existing high-entropy strings or test fixtures that
trip the entropy heuristic but are not real secrets. The baseline captures
the *current* set of findings to a JSON sidecar so subsequent scans only
report **net-new** findings — drift from the recorded state.

Workflow:

  1. Operator runs ``autodev secretscan baseline`` → ``compute_baseline``
     scans the full tree and writes ``.autodev/secretscan-baseline.json``.
  2. ``run_secretscan`` (when ``cfg.qa_gates.secretscan_baseline_enabled``)
     consults the baseline via ``filter_against_baseline`` and skips any
     finding whose *repo-relative file path + finding category* tuple is
     already recorded.
  3. Operator refreshes when intentionally accepting new findings.

The baseline is keyed by ``f"{rel_path}|{label}"`` — file path plus the
finding category (``"AWS access key"``, ``"high-entropy string"`` etc.).
The exact entropy bits or matched substring is not part of the key so
small textual edits to an already-baselined finding still pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from qa.secretscan import run_secretscan


_BASELINE_REL_PATH = Path(".autodev") / "secretscan-baseline.json"


def _baseline_path(cwd: Path) -> Path:
    return cwd / _BASELINE_REL_PATH


def _normalize_finding(finding: str) -> str:
    """Reduce a raw finding line to a stable baseline key.

    Findings have the shape ``"<rel_path>: <label> [— extra]"``. We collapse
    everything after the label to obtain a key insensitive to entropy bits
    or matched-substring snippets.
    """
    # Strip everything after the second ``: `` separator, then drop any
    # trailing em-dash narrative (e.g. " — abc1234…").
    parts = finding.split(": ", 1)
    if len(parts) != 2:
        return finding.strip()
    path, rest = parts
    # ``rest`` is "<label>" or "<label> ([…]) — extra".
    label_stop = rest.find(" (")
    if label_stop == -1:
        # Generic label without parens.
        em_dash = rest.find(" — ")
        label = rest[:em_dash] if em_dash != -1 else rest
    else:
        label = rest[:label_stop]
    return f"{path}|{label.strip()}"


async def compute_baseline(cwd: Path) -> set[str]:
    """Scan *cwd* and persist the baseline finding-key set.

    The full-tree scan honors the same ``.autodev/secretscan-allow``
    allowlist and per-extension entropy curves the regular gate uses. The
    persisted baseline becomes the bar against which future runs measure
    "is this a NEW finding?".

    Returns the computed key set. Caller may use it in-process before the
    file is re-read.
    """
    result = await run_secretscan(cwd)
    keys: set[str] = set()
    if not result.passed:
        # Findings detail: "potential secrets found:\nFINDING\nFINDING…\n…"
        details = result.details or ""
        for raw in details.splitlines():
            line = raw.strip()
            if not line or line.startswith("potential secrets"):
                continue
            if line.startswith("…"):
                continue
            keys.add(_normalize_finding(line))

    target = _baseline_path(cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"version": 1, "keys": sorted(keys)}, indent=2),
        encoding="utf-8",
    )
    return keys


def load_baseline(cwd: Path) -> set[str]:
    """Read the persisted baseline. Empty set if absent."""
    path = _baseline_path(cwd)
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    keys = raw.get("keys", []) if isinstance(raw, dict) else []
    return {str(k) for k in keys}


async def filter_against_baseline(
    findings: list[str], cwd: Path
) -> list[str]:
    """Return only findings whose key is NOT in the persisted baseline.

    When the baseline file is missing, returns *findings* unchanged
    (fail-open: a missing baseline must not silently mask findings).
    """
    baseline = load_baseline(cwd)
    if not baseline:
        return list(findings)
    out: list[str] = []
    for f in findings:
        if _normalize_finding(f) not in baseline:
            out.append(f)
    return out


__all__ = [
    "compute_baseline",
    "filter_against_baseline",
    "load_baseline",
]
