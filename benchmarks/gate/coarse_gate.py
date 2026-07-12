"""Coarse SWE-bench regression tripwire — the on-demand red/green gate (P1.5).

This is the last mile of the Phase-1 benchmark: given a completed run's
per-instance PASS/FAIL/ERROR verdicts (plus wall-time / quota-wait / blind
telemetry), decide whether AutoDev has *regressed* on the fixed ~15-20
SWE-bench-Lite slice versus a stored baseline.

Two hard properties, mirroring ``ci/release_preflight_greps.sh::preflight_v100``:

- **Anti-vacuity.** The gate must never pass on nothing. A degenerate slice — too
  few real capability verdicts (``completed < min``) or an ERROR-heavy slice
  (``error-rate > threshold``) — is RED, not a silent green. An empty slice is
  the extreme case and is RED. A degenerate slice is also never written as a
  baseline (it would poison every future comparison).
- **Broken control.** With ``AUTODEV_BENCH_FORCE_NULL_SOLVER=1`` in the
  environment the gate returns RED unconditionally — the planted-failure proof
  that the gate *can* fail (exactly the role ``AUTODEV_RESOLVER_FORCE_DISABLED``
  plays for the release engagement gate).

The comparison itself reuses :func:`benchmarks.runner.scorer.score_benchmark_results`
(the same baseline-diff helper the v1 harness uses) over a **resolve-rate**
computed on the *completed* subset (``passed / (passed + failed)``) — ERRORs are
infra/quota noise and are deliberately kept out of the capability denominator
(their prevalence is guarded separately by the anti-vacuity error-rate check), so
a quota-heavy night can never masquerade as a capability regression. The drop
threshold is deliberately **coarse** (~0.20-0.30) — sized to the large sampling
noise at N~=15-20 so only big regressions clear it (ADR-0050 / the plan).

Everything here is pure + hermetic: no network, no sb-cli, no autodev. The
baseline lives on disk under ``benchmarks/baselines/<slice>/<version>.json`` and
is read/written with the stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.runner.scorer import score_benchmark_results
from benchmarks.scorers.base import ERROR, FAIL, PASS, ScoreReport

# ---------------------------------------------------------------------------
# Named policy constants (the pilot, P1.6, tunes these against measured noise).
# ---------------------------------------------------------------------------

# The fixed Phase-1 slice name; the baseline lives under this sub-directory.
SLICE_NAME = "swebench-lite-phase1"

# Default root for stored baselines: ``benchmarks/baselines`` (sibling of the
# ``gate`` package). Tests point this at a tmp dir; nothing here writes the real
# tree unless a caller passes the default root explicitly.
DEFAULT_BASELINES_ROOT = Path(__file__).resolve().parent.parent / "baselines"

# COARSE resolve-rate drop that flags a regression. In the plan's 0.20-0.30 band
# and sized to N~=15-20 noise: only a *big* drop clears it. Small real
# regressions are invisible at Phase-1 — an accepted limitation (rigor is Phase 2).
COARSE_RESOLVE_DROP_THRESHOLD = 0.25

# Anti-vacuity floors. Fewer than this many completed (PASS+FAIL) capability
# verdicts, or a higher-than-this ERROR fraction, makes the slice untrustworthy
# and the gate RED regardless of the resolve rate.
DEFAULT_MIN_COMPLETED = 12
DEFAULT_MAX_ERROR_RATE = 0.30

# Known SWE-bench-Lite instances that are **unpassable on an OFFLINE eval host
# regardless of fix quality** because their required FAIL_TO_PASS tests make live
# network calls to ``httpbin.org``:
#
#   * ``psf__requests-1963`` — 6 of 7 F2P tests hit live httpbin.org
#   * ``psf__requests-2148`` — 9 of 10 F2P tests hit live httpbin.org
#
# (Their ``test_requests.py`` bodies call a ``httpbin(...)`` helper defaulting to
# ``http://httpbin.org/``; evidence in the slice-4 forensic dossiers A2/A9. AutoDev
# in fact produced APPROVED fixes for both — this is a BENCHMARK-HOST artifact, NOT
# an AutoDev solve defect.) They are excluded from capability accounting exactly
# like ``blind`` is: OUT of the resolve-rate denominator + the go/no-go cohort, so a
# scoring FAIL that only reflects "no network on the eval host" cannot masquerade as
# a real capability FAIL and depress the gate.
#
# CAVEATS (deliberately conservative + to-be-re-confirmed):
#   * ONLY these two — no wildcards, no whole-repo exclusion; every OTHER instance
#     (including other ``psf/requests`` instances) stays fully counted.
#   * Evidence-cited to the offline slice-4 run. This must be RE-CONFIRMED once the
#     sb-cli cloud grader is restored (WS-1) — the cloud grader may provide httpbin,
#     in which case these become normal, countable instances again.
KNOWN_NETWORK_ARTIFACT_INSTANCES: frozenset[str] = frozenset(
    {
        "psf__requests-1963",
        "psf__requests-2148",
    }
)

# The broken-control env var (set to "1" -> gate RED unconditionally).
FORCE_NULL_SOLVER_ENV = "AUTODEV_BENCH_FORCE_NULL_SOLVER"

# Baseline document schema tag (bump on an incompatible shape change).
BASELINE_SCHEMA = "coarse-gate-baseline/1"

# Verdicts + status labels (stable strings — consumed by the CLI/report, P1.6).
GREEN = "green"
RED = "red"

STATUS_BASELINE_ESTABLISHED = "baseline-established"
STATUS_HEALTHY = "healthy"
STATUS_REGRESSED = "regressed"
STATUS_INSUFFICIENT_COMPLETED = "insufficient-completed"
STATUS_ERROR_RATE_EXCEEDED = "error-rate-exceeded"
STATUS_BROKEN_CONTROL = "broken-control-null-solver"
STATUS_BASELINE_UNREADABLE = "baseline-unreadable"


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateInstance:
    """One instance's verdict + the telemetry the coarse report surfaces.

    ``status`` is one of ``PASS`` / ``FAIL`` / ``ERROR`` (the scorer vocabulary;
    ERROR is infra/quota, never a capability FAIL). ``quota_wait_time_s`` is how
    long the quota guard parked this instance; ``blind`` records whether it
    solved with ``test_runner`` off (arm64 deps failed to install).
    ``network_artifact`` records whether this is a known benchmark-host artifact
    (see :data:`KNOWN_NETWORK_ARTIFACT_INSTANCES`) that is excluded from the
    resolve-rate denominator + go/no-go — set by
    :func:`gate_instances_from_score_report`, exactly as ``blind`` is set.
    """

    instance_id: str
    status: str
    wall_time_s: float = 0.0
    quota_wait_time_s: float = 0.0
    blind: bool = False
    network_artifact: bool = False


@dataclass(frozen=True)
class CoarseGateConfig:
    """Tunable thresholds for the coarse gate (frozen so it is a safe default)."""

    min_completed: int = DEFAULT_MIN_COMPLETED
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE
    resolve_drop_threshold: float = COARSE_RESOLVE_DROP_THRESHOLD


@dataclass
class GateReport:
    """The gate's full verdict + the telemetry a human/CI reads.

    ``verdict`` is :data:`GREEN` / :data:`RED`; ``status`` is the specific label
    (e.g. :data:`STATUS_REGRESSED`); ``reasons`` lists every failing condition
    (there can be more than one, though the isolated gate tests trip exactly
    one). The count/telemetry fields are always populated so the report is
    printable regardless of verdict.
    """

    verdict: str
    status: str
    reasons: list[str]
    passed: int
    failed: int
    errored: int
    completed: int
    total: int
    resolve_rate: float
    error_rate: float
    blind_count: int
    total_wall_time_s: float
    total_quota_wait_time_s: float
    baseline_established: bool
    baseline_path: str
    per_instance: list[dict]
    blind_instances: list[str]
    network_artifact_instances: list[str]
    baseline_resolve_rate: float | None = None
    resolve_rate_drop: float | None = None
    comparison: dict | None = None

    @property
    def is_red(self) -> bool:
        return self.verdict == RED

    @property
    def is_green(self) -> bool:
        return self.verdict == GREEN


# ---------------------------------------------------------------------------
# Counting + doc shaping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Counts:
    passed: int
    failed: int
    errored: int
    completed: int
    total: int
    resolve_rate: float
    error_rate: float


def _count(instances: Sequence[GateInstance]) -> _Counts:
    """Tally PASS/FAIL/ERROR and derive the capability resolve-rate.

    ``completed = passed + failed`` (instances that reached a real verdict);
    ``resolve_rate = passed / completed`` (ERRORs excluded from the denominator —
    they are guarded separately by the error-rate check). ``error_rate`` is over
    the full slice.
    """
    passed = sum(1 for i in instances if i.status == PASS)
    errored = sum(1 for i in instances if i.status == ERROR)
    # Anything that is neither PASS nor ERROR is a capability FAIL (matches
    # ScoreReport.counts()): an unknown status is conservatively a FAIL, never a
    # silent resolve.
    failed = sum(1 for i in instances if i.status not in (PASS, ERROR))
    completed = passed + failed
    total = len(instances)
    resolve_rate = (passed / completed) if completed else 0.0
    error_rate = (errored / total) if total else 0.0
    return _Counts(
        passed=passed,
        failed=failed,
        errored=errored,
        completed=completed,
        total=total,
        resolve_rate=resolve_rate,
        error_rate=error_rate,
    )


def _score_doc(instances: Sequence[GateInstance]) -> dict:
    """Shape the *completed* (PASS/FAIL) instances into the doc
    :func:`score_benchmark_results` consumes.

    ``summary.total`` is set to the completed count (not the raw slice size) so
    the helper's ``passed / total`` yields the capability resolve-rate on the
    completed subset. ERROR instances are excluded — they carry no capability
    verdict to compare.
    """
    completed = [i for i in instances if i.status in (PASS, FAIL)]
    passed = sum(1 for i in completed if i.status == PASS)
    return {
        "summary": {"passed": passed, "total": len(completed)},
        "results": [
            {"task_id": i.instance_id, "status": i.status} for i in completed
        ],
    }


def _instance_dict(i: GateInstance) -> dict:
    return {
        "instance_id": i.instance_id,
        "status": i.status,
        "wall_time_s": i.wall_time_s,
        "quota_wait_time_s": i.quota_wait_time_s,
        "blind": i.blind,
    }


def _instances_from_baseline_doc(doc: Mapping[str, Any]) -> list[GateInstance]:
    """Reconstruct the baseline's :class:`GateInstance` list from its stored doc.

    Tolerant of either ``instance_id`` or ``task_id`` keys and missing telemetry,
    so an older/hand-written baseline still loads.
    """
    out: list[GateInstance] = []
    for row in doc.get("results", []) or []:
        if not isinstance(row, Mapping):
            continue
        iid = str(row.get("instance_id") or row.get("task_id") or "")
        out.append(
            GateInstance(
                instance_id=iid,
                status=str(row.get("status", "")),
                wall_time_s=float(row.get("wall_time_s", 0.0) or 0.0),
                quota_wait_time_s=float(row.get("quota_wait_time_s", 0.0) or 0.0),
                blind=bool(row.get("blind", False)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Baseline read / write
# ---------------------------------------------------------------------------


def baseline_path_for(baselines_root: Path, autodev_version: str) -> Path:
    """The on-disk baseline path for a given version: ``<root>/<slice>/<ver>.json``."""
    return baselines_root / SLICE_NAME / f"{autodev_version}.json"


def write_baseline(
    baselines_root: Path,
    autodev_version: str,
    instances: Sequence[GateInstance],
) -> Path:
    """Atomically write the Phase-1 baseline for ``autodev_version`` and return
    its path.

    The stored doc keeps the full per-instance detail (so a future run can
    reconstruct the completed subset for the comparison) plus a summary. Written
    via a temp file + ``os.replace`` so a crashed write never leaves a truncated
    baseline (idempotent-redundancy discipline).
    """
    counts = _count(instances)
    doc = {
        "schema": BASELINE_SCHEMA,
        "slice": SLICE_NAME,
        "autodev_version": autodev_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "passed": counts.passed,
            "failed": counts.failed,
            "errored": counts.errored,
            "completed": counts.completed,
            "total": counts.total,
            "resolve_rate": round(counts.resolve_rate, 4),
            "error_rate": round(counts.error_rate, 4),
        },
        "results": [_instance_dict(i) for i in instances],
    }
    path = baseline_path_for(baselines_root, autodev_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        # Never leave the temp file behind on a failed write.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def evaluate_coarse_gate(
    instances: Sequence[GateInstance],
    *,
    autodev_version: str,
    baselines_root: Path = DEFAULT_BASELINES_ROOT,
    config: CoarseGateConfig = CoarseGateConfig(),
    env: Mapping[str, str] | None = None,
) -> GateReport:
    """Return the coarse red/green verdict for one completed run.

    Order of decision (highest precedence first):

    1. **Broken control** — ``AUTODEV_BENCH_FORCE_NULL_SOLVER=1`` -> RED
       unconditionally (never establishes a baseline).
    2. **Anti-vacuity** — ``completed < min`` and/or ``error-rate > threshold``
       -> RED, and the degenerate slice is NOT written as a baseline.
    3. **No baseline** — establish it (write) and return GREEN
       (``baseline-established``): no gate on run 1.
    4. **Compare** — resolve-rate drop vs baseline ``>= threshold`` -> RED
       (``regressed``); otherwise GREEN (``healthy``).
    """
    env_map = os.environ if env is None else env
    # WS-7: known network-artifact instances (live-httpbin.org F2P tests,
    # unpassable offline — see :data:`KNOWN_NETWORK_ARTIFACT_INSTANCES`) are
    # excluded from the SCORED COHORT entirely: out of the resolve-rate
    # denominator, the anti-vacuity completed/error counts, the baseline
    # comparison, AND the written baseline (so they never poison a future
    # comparison). They are surfaced separately in ``network_artifact_instances``
    # (mirroring the ``blind_instances`` listing) — VISIBLE, never silently
    # dropped. This goes one step beyond ``blind``: a blind instance still carries
    # a real (if weak) verdict and stays IN the cohort, whereas a network artifact
    # carries no fair offline verdict at all and leaves it.
    network_artifact_instances = [
        i.instance_id for i in instances if i.network_artifact
    ]
    cohort = [i for i in instances if not i.network_artifact]
    counts = _count(cohort)
    per_instance = [_instance_dict(i) for i in cohort]
    blind_instances = [i.instance_id for i in cohort if i.blind]
    base_path = baseline_path_for(baselines_root, autodev_version)

    def _report(
        verdict: str,
        status: str,
        reasons: list[str],
        *,
        baseline_established: bool = False,
        baseline_resolve_rate: float | None = None,
        resolve_rate_drop: float | None = None,
        comparison: dict | None = None,
    ) -> GateReport:
        return GateReport(
            verdict=verdict,
            status=status,
            reasons=reasons,
            passed=counts.passed,
            failed=counts.failed,
            errored=counts.errored,
            completed=counts.completed,
            total=counts.total,
            resolve_rate=counts.resolve_rate,
            error_rate=counts.error_rate,
            blind_count=len(blind_instances),
            total_wall_time_s=sum(i.wall_time_s for i in cohort),
            total_quota_wait_time_s=sum(i.quota_wait_time_s for i in cohort),
            baseline_established=baseline_established,
            baseline_path=str(base_path),
            per_instance=per_instance,
            blind_instances=blind_instances,
            network_artifact_instances=network_artifact_instances,
            baseline_resolve_rate=baseline_resolve_rate,
            resolve_rate_drop=resolve_rate_drop,
            comparison=comparison,
        )

    # 1. Broken control — highest precedence. Proves the gate can emit RED even
    #    on an otherwise-healthy slice.
    if str(env_map.get(FORCE_NULL_SOLVER_ENV, "")) == "1":
        return _report(
            RED, STATUS_BROKEN_CONTROL, [STATUS_BROKEN_CONTROL]
        )

    # 2. Anti-vacuity — a degenerate slice is RED and must not become a baseline.
    vacuity_reasons: list[str] = []
    if counts.completed < config.min_completed:
        vacuity_reasons.append(STATUS_INSUFFICIENT_COMPLETED)
    if counts.error_rate > config.max_error_rate:
        vacuity_reasons.append(STATUS_ERROR_RATE_EXCEEDED)
    if vacuity_reasons:
        return _report(RED, vacuity_reasons[0], vacuity_reasons)

    # 3. No baseline -> establish it (the slice is trustworthy here) + GREEN. The
    #    baseline is written over the COHORT (network artifacts excluded) so it can
    #    never seed a future comparison with a verdict the offline host can't fairly
    #    produce.
    if not base_path.is_file():
        write_baseline(baselines_root, autodev_version, cohort)
        return _report(
            GREEN,
            STATUS_BASELINE_ESTABLISHED,
            [],
            baseline_established=True,
        )

    # 4. A baseline IS present (step 3 established the file exists). Read + parse
    #    it. A PRESENT-but-corrupt baseline is RED (``baseline-unreadable``) — it
    #    must NEVER be silently degraded to an empty 0.0-rate baseline, which would
    #    make any current rate look like an "improvement" (drop <= 0) and mask a
    #    total capability collapse as a healthy green. (The genuine "no baseline
    #    exists yet" path is step 3 above — file absent -> establish + GREEN.)
    try:
        baseline_doc = json.loads(base_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _report(
            RED, STATUS_BASELINE_UNREADABLE, [STATUS_BASELINE_UNREADABLE]
        )
    baseline_instances = _instances_from_baseline_doc(baseline_doc) if isinstance(
        baseline_doc, Mapping
    ) else []
    # A structurally-usable baseline must carry at least one real capability
    # verdict; a present file with none is corrupt/degenerate, not a 0.0 baseline.
    if not any(i.status in (PASS, FAIL) for i in baseline_instances):
        return _report(
            RED, STATUS_BASELINE_UNREADABLE, [STATUS_BASELINE_UNREADABLE]
        )

    comparison = score_benchmark_results(
        _score_doc(cohort),
        _score_doc(baseline_instances),
        pass_rate_drop_threshold=config.resolve_drop_threshold,
    )
    baseline_resolve_rate = _as_float(comparison.get("baseline_pass_rate"))
    drop: float | None = (
        baseline_resolve_rate - counts.resolve_rate
        if baseline_resolve_rate is not None
        else None
    )

    # NOTE: ``report.verdict``/``status`` is AUTHORITATIVE. The gate trips on
    # ``drop >= threshold`` (inclusive of the boundary), whereas the reused
    # ``comparison['regressed']`` uses a strict ``delta < -threshold`` and so
    # disagrees only at the exact boundary ``drop == threshold``; ``comparison`` is
    # carried as auxiliary telemetry, never as the pass/fail decision.
    if drop is not None and drop >= config.resolve_drop_threshold:
        return _report(
            RED,
            STATUS_REGRESSED,
            [STATUS_REGRESSED],
            baseline_resolve_rate=baseline_resolve_rate,
            resolve_rate_drop=drop,
            comparison=comparison,
        )

    return _report(
        GREEN,
        STATUS_HEALTHY,
        [],
        baseline_resolve_rate=baseline_resolve_rate,
        resolve_rate_drop=drop,
        comparison=comparison,
    )


def _as_float(value: Any) -> float | None:
    """Coerce a comparison rate to ``float`` (``None`` stays ``None``)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Wiring helpers (genuine reuse of the sibling pipeline objects — P1.6)
# ---------------------------------------------------------------------------


def gate_instances_from_score_report(
    report: ScoreReport,
    *,
    wall_times: Mapping[str, float] | None = None,
    quota_waits: Mapping[str, float] | None = None,
    blind: Mapping[str, bool] | None = None,
) -> list[GateInstance]:
    """Join the scorer's per-instance PASS/FAIL/ERROR verdicts with the solve-side
    telemetry (wall-time from the adapter's ``InstanceReport`` /
    ``SolveOutcome.wall_time_s``; quota-wait from the quota guard's
    ``GuardResult``; blind from ``InstanceReport.degraded_blind``) into the
    :class:`GateInstance` list the gate consumes. Telemetry is matched by
    ``instance_id``; anything missing defaults to zero / ``False``.

    ``network_artifact`` is set from :data:`KNOWN_NETWORK_ARTIFACT_INSTANCES`
    membership (WS-7) — the same construction-boundary at which ``blind`` is
    applied from the blind map — so the gate cohort excludes the two known
    live-httpbin.org instances without the caller wiring anything.
    """
    wt = wall_times or {}
    qw = quota_waits or {}
    bl = blind or {}
    return [
        GateInstance(
            instance_id=s.instance_id,
            status=s.status,
            wall_time_s=float(wt.get(s.instance_id, 0.0)),
            quota_wait_time_s=float(qw.get(s.instance_id, 0.0)),
            blind=bool(bl.get(s.instance_id, False)),
            network_artifact=s.instance_id in KNOWN_NETWORK_ARTIFACT_INSTANCES,
        )
        for s in report.instances
    ]


def current_autodev_version() -> str:
    """Best-effort current AutoDev version string for the default baseline path.

    Lazy + defensive (imports inside the function so importing this module stays
    hermetic even where ``src`` is not on the path): prefer the in-repo
    ``_version.__version__``, fall back to the installed package metadata, then a
    sentinel. Only used for real CLI/pilot runs (P1.6) — the gate itself always
    takes ``autodev_version`` explicitly.
    """
    try:
        from _version import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:  # noqa: BLE001 - src not importable in some contexts
        pass
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("ai-autodev")
    except Exception:  # noqa: BLE001 - package not installed
        return "0+unknown"


__all__ = [
    "BASELINE_SCHEMA",
    "COARSE_RESOLVE_DROP_THRESHOLD",
    "DEFAULT_BASELINES_ROOT",
    "DEFAULT_MAX_ERROR_RATE",
    "DEFAULT_MIN_COMPLETED",
    "FORCE_NULL_SOLVER_ENV",
    "GREEN",
    "KNOWN_NETWORK_ARTIFACT_INSTANCES",
    "RED",
    "SLICE_NAME",
    "STATUS_BASELINE_ESTABLISHED",
    "STATUS_BASELINE_UNREADABLE",
    "STATUS_BROKEN_CONTROL",
    "STATUS_ERROR_RATE_EXCEEDED",
    "STATUS_HEALTHY",
    "STATUS_INSUFFICIENT_COMPLETED",
    "STATUS_REGRESSED",
    "CoarseGateConfig",
    "GateInstance",
    "GateReport",
    "baseline_path_for",
    "current_autodev_version",
    "evaluate_coarse_gate",
    "gate_instances_from_score_report",
    "write_baseline",
]
