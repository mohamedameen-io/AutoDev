"""Phase-1 pilot runner — the FIRST operational deliverable (P1.6).

The pilot's job is **measurement**, not a gate. Given a candidate SWE-bench-Lite
instance list it drives the whole Phase-1 pipeline once, serially, over each
instance and records what actually happened:

    candidate instances
        -> host-arm64 adapter solve THROUGH the quota guard (P1.4)   [solve]
        -> sb-cli cloud scorer (P1.3)                                 [score]
        -> coarse gate / baseline-establish (P1.5)                    [gate]

For every instance it records a per-instance status (PASS / FAIL / ERROR, with
the quota guard's **ERROR-until-complete** rule enforced so a quota abort is NEVER
a false FAIL), the solve wall-time, the quota-wait time it parked on the
subscription cap, and whether it solved *blind* (arm64 deps failed → ``test_runner``
off, no self-repair) versus with self-repair. It writes a pilot report (JSON +
human summary) and, on a **healthy** first run, emits the first baseline JSON via
the gate's baseline-establish path.

Three design rules keep this honest and testable:

- **Everything heavy is injected.** The quota-aware solve loop (:func:`run_guarded_
  solve`), the scorer, and the gate function are all parameters with real
  defaults, so the whole runner is unit-testable with in-memory fakes — no
  autodev, no sb-cli, no network, no sleep. The concrete SWE-bench-Lite
  adapter/dataset/scorer are imported **lazily inside** :func:`main` (never at
  module load), mirroring :mod:`benchmarks.runner.external`.
- **ERROR-until-complete, never a false FAIL.** The quota guard already guarantees
  a quota abort is COMPLETE-with-a-real-outcome or ERROR (quota-exhausted), never
  FAIL. :func:`pilot_instance_status` re-asserts it: a quota-exhausted (or any
  guard-ERROR) instance is ERROR regardless of any scorer verdict.
- **No hard-coded instance IDs.** :func:`select_candidate_instances` loads
  instances from the dataset loader (P1.2) and applies a documented heuristic that
  *prefers* lighter-dependency / pure-python repos (more likely to build an arm64
  venv). The operator supplies the dataset (``pip install datasets`` or a local
  JSONL) — see ``benchmarks/RUNBOOK-phase1-pilot.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from benchmarks.adapters.base import BenchmarkAdapter, Instance
from benchmarks.gate.coarse_gate import (
    DEFAULT_BASELINES_ROOT,
    CoarseGateConfig,
    GateReport,
    current_autodev_version,
    evaluate_coarse_gate,
    gate_instances_from_score_report,
)
from benchmarks.runner.quota_guard import (
    DEFAULT_MAX_ATTEMPTS,
    BackoffPolicy,
    GuardResult,
    QuotaWaitEvent,
    SleepFn,
    run_guarded_solve,
)
from benchmarks.runner.solve import SolveInvoker, default_solve_invoker
from benchmarks.scorers.base import ERROR, FAIL, PASS, ScoreReport, Scorer

# ---------------------------------------------------------------------------
# Candidate selection heuristic (no hard-coded instance IDs)
# ---------------------------------------------------------------------------

# Default candidate screen size — the pilot screens ~25-30 instances down to the
# fixed ~15-20 slice (the plan). Configurable via ``--count``.
DEFAULT_CANDIDATE_COUNT = 30

# Repos with heavy native / C-extension build chains that frequently fail to build
# a per-instance **arm64** venv (the Phase-1 host-solve environment). Instances on
# these repos are *deprioritised* by :func:`select_candidate_instances` so the
# pilot prefers pure-python repos that are more likely to install deps (and so keep
# ``test_runner`` ON / self-repair engaged rather than solving blind). This is a
# documented, deliberately-coarse HEURISTIC, not a guarantee — the pilot's whole
# point is to *measure* which instances actually build. Substring-matched against
# the lower-cased ``repo``; injectable so the operator can widen/narrow it without
# editing this module.
HEAVY_DEP_REPO_HINTS: frozenset[str] = frozenset(
    {
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "pillow",
        "scikit-learn",
        "scikit-image",
        "tensorflow",
        "pytorch",
        "torch",
        "lxml",
        "psycopg2",
        "cryptography",
        "numba",
        "cython",
        "shapely",
        "cartopy",
        "h5py",
        "pyarrow",
        "grpcio",
    }
)

# Exact SWE-bench-Lite repo slugs whose native / transitive (numpy, C-extension,
# freetype/openmp) build chains empirically fail a per-instance arm64 venv build,
# so their instances only ever solve *blind*. HEAVY_DEP_REPO_HINTS above only
# catches repos whose slug literally contains the dep name (matplotlib,
# scikit-learn); these slugs catch the rest — astropy proved this in the Phase-1
# smoke (all 3 picks were astropy, all blind), seaborn pulls matplotlib+scipy,
# xarray pulls numpy+pandas. Injectable like the hints. django/sympy/flask/
# requests/pylint/pytest/sphinx are pure-python and stay in the friendly tier.
HEAVY_LITE_REPOS: frozenset[str] = frozenset(
    {
        "astropy/astropy",
        "matplotlib/matplotlib",
        "mwaskom/seaborn",
        "scikit-learn/scikit-learn",
        "pydata/xarray",
    }
)


def _is_heavy_repo(
    instance: Instance,
    heavy_hints: frozenset[str],
    heavy_repos: frozenset[str] = HEAVY_LITE_REPOS,
) -> bool:
    """True iff the instance's ``repo`` is a known heavy native-build repo.

    Two signals: an exact ``heavy_repos`` slug match (the reliable one for the
    fixed SWE-bench-Lite repo set), OR a ``heavy_hints`` dependency-name substring
    match (a coarse fallback for arbitrary datasets).
    """
    repo = str(instance.get("repo", "")).lower()
    if not repo:
        return False
    if repo in heavy_repos:
        return True
    return any(hint in repo for hint in heavy_hints)


def _round_robin_by_repo(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interleave instances across their ``repo`` (repo insertion order preserved)
    so a tight ``count`` spreads the slice across repos instead of exhausting the
    first (alphabetically-earliest, often largest) repo — a more representative
    benchmark slice and a broader arm64-buildability signal."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for inst in instances:
        repo = str(inst.get("repo", "")).lower()
        if repo not in buckets:
            buckets[repo] = []
            order.append(repo)
        buckets[repo].append(inst)
    out: list[dict[str, Any]] = []
    depth = 0
    remaining = True
    while remaining:
        remaining = False
        for repo in order:
            bucket = buckets[repo]
            if depth < len(bucket):
                out.append(bucket[depth])
                remaining = True
        depth += 1
    return out


def select_candidate_instances(
    instances: Iterable[Instance],
    *,
    count: int = DEFAULT_CANDIDATE_COUNT,
    heavy_hints: frozenset[str] = HEAVY_DEP_REPO_HINTS,
    heavy_repos: frozenset[str] = HEAVY_LITE_REPOS,
) -> list[dict[str, Any]]:
    """Pick up to ``count`` arm64-friendly candidate instances.

    Steps, in order:

    1. **De-duplicate** by ``instance_id`` (first occurrence wins); drop records
       with an empty ``instance_id`` (they cannot be scored).
    2. **Prefer pure-python repos**: partition into a friendly tier and a heavy
       native-build tier (see :func:`_is_heavy_repo` — exact :data:`HEAVY_LITE_REPOS`
       slug match OR :data:`HEAVY_DEP_REPO_HINTS` dep-name substring). The friendly
       tier comes first, so a tight ``count`` drops the heavy repos (most likely to
       fail an arm64 venv build and only ever solve blind) first.
    3. **Spread across repos**: round-robin within each tier so a tight ``count``
       samples many repos instead of exhausting the first (often largest) one — a
       more representative slice and a broader arm64-buildability signal.
    4. **Truncate** to ``count``.

    Returns plain dicts (a shallow copy of each selected instance) so downstream
    mutation never aliases the caller's dataset. Never hits the network — the
    caller supplies the loaded instances (see :func:`main` for the lazy dataset
    load).
    """
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for inst in instances:
        iid = str(inst.get("instance_id", "")).strip()
        if not iid or iid in seen:
            continue
        seen.add(iid)
        deduped.append(dict(inst))

    # Partition by arm64-friendliness (pure-python first), then round-robin within
    # each tier so a tight count both drops heavy native-build repos first AND
    # spreads across repos rather than exhausting one big repo.
    light = [i for i in deduped if not _is_heavy_repo(i, heavy_hints, heavy_repos)]
    heavy = [i for i in deduped if _is_heavy_repo(i, heavy_hints, heavy_repos)]
    ordered = _round_robin_by_repo(light) + _round_robin_by_repo(heavy)
    if count >= 0:
        return ordered[:count]
    return ordered


# ---------------------------------------------------------------------------
# Per-instance status: ERROR-until-complete (a quota wait is never a FAIL)
# ---------------------------------------------------------------------------


def pilot_instance_status(guard_result: GuardResult, score_status: str) -> str:
    """Map a quota-guard result + the scorer's verdict to the pilot status.

    The one invariant: a quota abort is **never** a capability FAIL. When the guard
    did not reach a real capability outcome — it is quota-exhausted, or otherwise
    terminated ERROR (still in-flight w.r.t. the quota window) — the pilot status
    is :data:`ERROR`, *independent of* whatever ``score_status`` says. Only when the
    guard reached a real terminal outcome (:data:`~benchmarks.runner.quota_guard.COMPLETE`)
    does the scorer's PASS / FAIL / ERROR verdict stand.
    """
    if guard_result.quota_exhausted or guard_result.status == ERROR:
        return ERROR
    return score_status


# ---------------------------------------------------------------------------
# Report shapes
# ---------------------------------------------------------------------------

# Go/no-go floor: the pilot recommends locking the slice + baseline when at least
# this many instances ran *cleanly* (a real PASS/FAIL verdict, deps installed so
# self-repair engaged — not blind, not ERROR). Below it, arm64 dep failures are
# likely pervasive → escalate the constraint (see the runbook). Sized to the plan's
# "≥~15 clean instances" go criterion.
GO_NOGO_MIN_CLEAN = 15

# Length (chars) of the fail_stdout_tail/fail_stderr_tail EXCERPT rendered in the
# human summary's failure-detail section — a short pointer for at-a-glance triage,
# not the full capture (up to solve.py's _FAIL_OUTPUT_TAIL=2000 chars). The FULL
# tail is always in pilot-report.json.
_SUMMARY_TAIL_EXCERPT_CHARS = 300

_BACKTICK_RUN_RE = re.compile(r"`+")


def _render_tail_block(lines: list[str], *, label: str, tail: str) -> None:
    """Append one excerpt block (``stdout``/``stderr``) to ``lines`` as a
    fenced code block, sized against ``_SUMMARY_TAIL_EXCERPT_CHARS``.

    Code-review finding: captured autodev output routinely contains its own
    triple-backtick fences (autodev echoes diffs/plans/markdown), so a
    hardcoded ``` fence can be closed early by the CONTENT, garbling the
    rendered section (the raw file / JSON are unaffected — only the
    rendering breaks). Widening the fence to one backtick longer than the
    longest backtick run actually present in the excerpt makes this
    unreachable, mirroring the standard Markdown nested-fence convention.
    """
    excerpt = tail[-_SUMMARY_TAIL_EXCERPT_CHARS:]
    longest_run = max((len(m) for m in _BACKTICK_RUN_RE.findall(excerpt)), default=0)
    fence = "`" * max(longest_run + 1, 3)
    lines.append(f"{label} (last {len(excerpt)} chars; full tail in pilot-report.json):")
    lines.append(fence)
    lines.append(excerpt)
    lines.append(fence)
    lines.append("")


@dataclass
class PilotInstanceOutcome:
    """One instance's fully-recorded pilot result.

    ``status`` is PASS / FAIL / ERROR with the ERROR-until-complete rule applied
    (see :func:`pilot_instance_status`). ``blind`` records whether the instance
    solved with ``test_runner`` off (arm64 deps failed → no self-repair). The
    remaining fields are the throughput/quota telemetry the pilot exists to
    measure.

    ``fail_stdout_tail`` / ``fail_stderr_tail`` are threaded straight from the
    terminal :class:`~benchmarks.runner.quota_guard.GuardResult` (default ``""``
    when nothing was captured — a clean PASS, or a prepare-error isolation) so a
    timeout/error is diagnosable from the pilot report alone, without
    hand-inspecting the instance's workdir. ``PilotReport.to_dict()`` serialises
    the FULL tails (via ``asdict()``); ``human_summary()`` renders only a short
    excerpt of each (see ``_SUMMARY_TAIL_EXCERPT_CHARS``).

    ``install_stdout_tail`` / ``install_stderr_tail`` are threaded the same way
    but sourced from the adapter's ``InstanceReport`` (see :func:`_install_tail_map`,
    which mirrors :func:`_blind_map`'s read of ``degraded_blind``) — the
    per-instance arm64-install-failure capture (WS-7), diagnosing WHY an instance
    went blind rather than just recording that it did.

    ``score_detail`` is the SCORE-side counterpart to ``detail`` (which is the
    SOLVE-side/quota-guard detail): it is the scorer's own
    :attr:`~benchmarks.scorers.base.InstanceScore.detail` for this instance (e.g.
    "sb-cli eval did not complete (infra)" — WS-1), always recorded regardless of
    whether :func:`pilot_instance_status` overrode the final ``status``, so a
    scoring-side infra failure is diagnosable from the pilot report alone.

    ``cost_usd`` is the instance's total spend, summed from every line of the
    solved workdir's ``run-summary.jsonl`` (the sibling of the terminal
    ``SolveOutcome.ledger_path`` — see :func:`_instance_cost_usd`); ``0.0`` when
    no outcome/ledger exists or nothing was recorded (best-effort, never raises).
    """

    instance_id: str
    status: str
    wall_time_s: float
    quota_wait_time_s: float
    attempts: int
    blind: bool
    quota_exhausted: bool
    detail: str | None = None
    fail_stdout_tail: str = ""
    fail_stderr_tail: str = ""
    install_stdout_tail: str = ""
    install_stderr_tail: str = ""
    score_detail: str | None = None
    cost_usd: float = 0.0

    @property
    def clean(self) -> bool:
        """A real capability verdict reached with self-repair on (not blind)."""
        return self.status in (PASS, FAIL) and not self.blind


@dataclass
class PilotReport:
    """The pilot's full record: per-instance outcomes + aggregate throughput +
    the gate verdict / baseline decision. Serialisable to JSON and to a human
    summary via :func:`write_pilot_report`.
    """

    run_id: str
    autodev_version: str
    timestamp: str
    instances: list[PilotInstanceOutcome]
    passed: int
    failed: int
    errored: int
    blind_count: int
    clean_count: int
    total_wall_time_s: float
    total_quota_wait_time_s: float
    gate_verdict: str
    gate_status: str
    gate_reasons: list[str]
    baseline_established: bool
    baseline_path: str | None
    recommend_lock: bool
    quota_wait_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "autodev_version": self.autodev_version,
            "timestamp": self.timestamp,
            "summary": {
                "total": len(self.instances),
                "passed": self.passed,
                "failed": self.failed,
                "errored": self.errored,
                "blind_count": self.blind_count,
                "clean_count": self.clean_count,
                "total_wall_time_s": round(self.total_wall_time_s, 3),
                "total_quota_wait_time_s": round(self.total_quota_wait_time_s, 3),
            },
            "gate": {
                "verdict": self.gate_verdict,
                "status": self.gate_status,
                "reasons": self.gate_reasons,
                "baseline_established": self.baseline_established,
                "baseline_path": self.baseline_path,
            },
            "go_no_go": {
                "min_clean": GO_NOGO_MIN_CLEAN,
                "clean_count": self.clean_count,
                "recommend_lock": self.recommend_lock,
            },
            "instances": [asdict(o) for o in self.instances],
            "quota_wait_events": self.quota_wait_events,
        }

    def human_summary(self) -> str:
        """A compact, greppable text summary (the ``.md`` the operator reads)."""
        lines: list[str] = []
        lines.append(f"# Phase-1 pilot report — {self.run_id}")
        lines.append("")
        lines.append(f"- autodev_version: {self.autodev_version}")
        lines.append(f"- timestamp: {self.timestamp}")
        lines.append(
            f"- instances: {len(self.instances)} "
            f"(PASS={self.passed} FAIL={self.failed} ERROR={self.errored})"
        )
        lines.append(
            f"- blind (deps failed, self-repair off): {self.blind_count}; "
            f"clean (verdict + self-repair): {self.clean_count}"
        )
        lines.append(
            f"- wall-time: {self.total_wall_time_s:.0f}s; "
            f"quota-wait: {self.total_quota_wait_time_s:.0f}s"
        )
        lines.append(
            f"- gate: {self.gate_verdict.upper()} ({self.gate_status}); "
            f"baseline_established={self.baseline_established}"
        )
        if self.baseline_path:
            lines.append(f"- baseline: {self.baseline_path}")
        lines.append(
            f"- GO/NO-GO: clean={self.clean_count} >= min={GO_NOGO_MIN_CLEAN} "
            f"-> recommend_lock={self.recommend_lock}"
        )
        lines.append("")
        lines.append("## Per-instance")
        lines.append("")
        header = (
            "instance_id | status | wall_s | quota_wait_s | attempts | blind | "
            "cost_usd"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for o in self.instances:
            lines.append(
                f"{o.instance_id} | {o.status} | {o.wall_time_s:.1f} | "
                f"{o.quota_wait_time_s:.1f} | {o.attempts} | {o.blind} | "
                f"{o.cost_usd:.4f}"
            )
        lines.append("")

        # A separate section (not inlined into the pipe-delimited table above,
        # which multi-line stdout/stderr would corrupt). Data-driven — NOT
        # hardcoded to "only ERROR status instances" — so it can never silently
        # drift out of sync with however status is actually computed elsewhere.
        #
        # ``score_detail`` is gated on a non-PASS status: the real SbcliScorer
        # sets a non-empty detail on EVERY verdict — including "resolved" for a
        # PASS — so surfacing it unconditionally would list every healthy pass
        # under "Failure detail" and defeat the section on a green run. A PASS's
        # score_detail is still preserved in to_dict()/JSON; it is only omitted
        # from THIS human-summary section. The nested helper keeps the filter and
        # the render clause in lock-step (one definition, no drift).
        def _surfaces_score_detail(o: "PilotInstanceOutcome") -> bool:
            return bool(o.score_detail) and o.status != PASS

        lines.append("## Failure detail (excerpt — full tails in pilot-report.json)")
        lines.append("")
        reportable = [
            o
            for o in self.instances
            if o.detail
            or _surfaces_score_detail(o)
            or o.fail_stdout_tail
            or o.fail_stderr_tail
            or o.install_stdout_tail
            or o.install_stderr_tail
        ]
        if not reportable:
            lines.append("(none)")
            lines.append("")
        for o in reportable:
            lines.append(f"### {o.instance_id}")
            lines.append("")
            if o.detail:
                lines.append(f"- detail: {o.detail}")
                lines.append("")
            if _surfaces_score_detail(o):
                lines.append(f"- score_detail: {o.score_detail}")
                lines.append("")
            if o.fail_stdout_tail:
                _render_tail_block(lines, label="stdout", tail=o.fail_stdout_tail)
            if o.fail_stderr_tail:
                _render_tail_block(lines, label="stderr", tail=o.fail_stderr_tail)
            # WS-7: the arm64 install-failure capture is a DISTINCT pipeline
            # stage from the solve-fail tails above, so it carries its own
            # "install stdout"/"install stderr" labels — this is often the ONLY
            # diagnostic for a blind instance (deps failed before a solve).
            if o.install_stdout_tail:
                _render_tail_block(
                    lines, label="install stdout", tail=o.install_stdout_tail
                )
            if o.install_stderr_tail:
                _render_tail_block(
                    lines, label="install stderr", tail=o.install_stderr_tail
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

# Type of an injectable quota-aware solve loop (default: run_guarded_solve).
GuardedSolveFn = Callable[..., "tuple[list[dict], list[GuardResult]]"]
# Type of an injectable gate evaluator (default: evaluate_coarse_gate).
GateFn = Callable[..., GateReport]


def _blind_map(adapter: BenchmarkAdapter) -> dict[str, bool]:
    """Read the adapter's per-instance ``InstanceReport``s for the blind flag.

    Defensive: an adapter without ``reports`` (a test double / a future adapter)
    simply yields an empty map → every instance defaults to not-blind.
    """
    out: dict[str, bool] = {}
    for rep in getattr(adapter, "reports", []) or []:
        iid = getattr(rep, "instance_id", None)
        if iid is None:
            continue
        out[str(iid)] = bool(getattr(rep, "degraded_blind", False))
    return out


def _install_tail_map(adapter: BenchmarkAdapter) -> dict[str, tuple[str, str]]:
    """Read the adapter's per-instance ``InstanceReport``s for the install-failure
    tails (WS-7) — mirrors :func:`_blind_map`'s defensive read of ``degraded_blind``
    exactly, just for the ``install_stdout_tail``/``install_stderr_tail`` pair.

    Defensive: an adapter without ``reports``, or an ``InstanceReport`` shape
    without these fields (an older report / a future adapter), simply yields
    ``("", "")`` for that instance.
    """
    out: dict[str, tuple[str, str]] = {}
    for rep in getattr(adapter, "reports", []) or []:
        iid = getattr(rep, "instance_id", None)
        if iid is None:
            continue
        out[str(iid)] = (
            str(getattr(rep, "install_stdout_tail", "") or ""),
            str(getattr(rep, "install_stderr_tail", "") or ""),
        )
    return out


def _instance_cost_usd(ledger_path: Path | None) -> float:
    """Sum ``cost_usd`` across every line of an instance's ``run-summary.jsonl``.

    ``run-summary.jsonl`` is written by the autodev CLI itself (once per
    ``plan``/``execute`` command; see ``src/state/run_summary.py``) as a SIBLING
    of the per-instance ledger, both under the solved workdir's ``.autodev/``
    directory — so its path is derived from ``ledger_path.parent``, never
    imported from autodev's own state package (mirroring how this whole runner
    treats autodev as a subprocess/artifact producer, never a library import;
    see ``quota_guard._ledger_has_signal`` for the same pattern against the
    ledger itself).

    Best-effort and defensive: ``ledger_path=None`` (no outcome — e.g. a raised
    quota abort or a prepare failure), a missing file, an unparseable line, OR a
    line that is valid JSON but NOT an object (``null``, a bare scalar, an array
    — exactly what a truncated / interleaved concurrent write produces) never
    raises — each simply contributes ``0.0``, so a telemetry gap can never crash
    the pilot (this runs unguarded inside ``run_pilot``'s per-instance loop, so a
    raise here would abort the whole pilot and write no report) or manufacture
    spend that was not actually recorded.
    """
    if ledger_path is None:
        return 0.0
    summary_path = ledger_path.parent / "run-summary.jsonl"
    if not summary_path.is_file():
        return 0.0
    total = 0.0
    try:
        text = summary_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        # Valid JSON but not an object (null / scalar / array) has no ``.get`` —
        # skip it rather than letting an AttributeError escape (the whole point
        # of this helper is that it NEVER raises).
        if not isinstance(row, dict):
            continue
        try:
            total += float(row.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def run_pilot(
    adapter: BenchmarkAdapter,
    scorer: Scorer,
    instances: Sequence[Instance],
    invoker: SolveInvoker,
    *,
    workdir_root: Path,
    run_id: str,
    autodev_version: str,
    baselines_root: Path = DEFAULT_BASELINES_ROOT,
    guarded_solve: GuardedSolveFn = run_guarded_solve,
    gate_fn: GateFn = evaluate_coarse_gate,
    gate_config: CoarseGateConfig = CoarseGateConfig(),
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: BackoffPolicy = BackoffPolicy(),
    sleep: SleepFn = time.sleep,
) -> PilotReport:
    """Run the pilot end-to-end over ``instances`` and return a :class:`PilotReport`.

    1. **Solve** every instance serially through the quota guard
       (``guarded_solve``): each solve forces ``max_parallel_subprocesses = 1`` and
       a quota abort is retried across quota windows (ERROR-until-complete), never
       FAILed. Returns ``(predictions, guard_results)``.
    2. **Score** the predictions with ``scorer`` (sb-cli in production) → a
       per-instance PASS / FAIL / ERROR :class:`ScoreReport`.
    3. **Record** per-instance outcomes joining the scorer verdict (via
       :func:`pilot_instance_status`, so a quota-exhausted instance stays ERROR)
       with the guard's wall-time / quota-wait telemetry and the adapter's blind
       flag.
    4. **Gate**: hand the joined verdicts to ``gate_fn`` (the coarse gate). On a
       healthy first run with no stored baseline the gate WRITES the first baseline
       under ``baselines_root`` (baseline-established → green); a degenerate slice
       is RED and writes nothing (anti-vacuity).

    Everything heavy is injected, so this is fully hermetic under test.
    """
    quota_wait_events: list[QuotaWaitEvent] = []

    predictions, guard_results = guarded_solve(
        adapter,
        instances,
        invoker,
        workdir_root=workdir_root,
        max_attempts=max_attempts,
        backoff=backoff,
        sleep=sleep,
        on_quota_wait=quota_wait_events.append,
    )

    score_report: ScoreReport = scorer.score(predictions, run_id=run_id)
    score_index = {s.instance_id: s for s in score_report.instances}

    wall_times: dict[str, float] = {}
    quota_waits: dict[str, float] = {}
    for g in guard_results:
        wall_times[g.instance_id] = (
            g.outcome.wall_time_s if g.outcome is not None else 0.0
        )
        quota_waits[g.instance_id] = g.quota_wait_time_s
    blind = _blind_map(adapter)
    install_tails = _install_tail_map(adapter)

    outcomes: list[PilotInstanceOutcome] = []
    for g in guard_results:
        score = score_index.get(g.instance_id)
        score_status = score.status if score is not None else ERROR
        install_stdout_tail, install_stderr_tail = install_tails.get(
            g.instance_id, ("", "")
        )
        outcomes.append(
            PilotInstanceOutcome(
                instance_id=g.instance_id,
                status=pilot_instance_status(g, score_status),
                wall_time_s=wall_times[g.instance_id],
                quota_wait_time_s=quota_waits[g.instance_id],
                attempts=g.attempts,
                blind=blind.get(g.instance_id, False),
                quota_exhausted=g.quota_exhausted,
                detail=g.detail,
                fail_stdout_tail=g.fail_stdout_tail,
                fail_stderr_tail=g.fail_stderr_tail,
                install_stdout_tail=install_stdout_tail,
                install_stderr_tail=install_stderr_tail,
                score_detail=score.detail if score is not None else None,
                cost_usd=_instance_cost_usd(
                    g.outcome.ledger_path if g.outcome is not None else None
                ),
            )
        )

    # Gate over the scorer's verdicts joined with the solve-side telemetry. The
    # gate writes the first baseline on a healthy no-baseline run.
    gate_instances = gate_instances_from_score_report(
        score_report,
        wall_times=wall_times,
        quota_waits=quota_waits,
        blind=blind,
    )
    gate_report = gate_fn(
        gate_instances,
        autodev_version=autodev_version,
        baselines_root=baselines_root,
        config=gate_config,
    )

    passed = sum(1 for o in outcomes if o.status == PASS)
    errored = sum(1 for o in outcomes if o.status == ERROR)
    failed = len(outcomes) - passed - errored
    blind_count = sum(1 for o in outcomes if o.blind)
    clean_count = sum(1 for o in outcomes if o.clean)

    return PilotReport(
        run_id=run_id,
        autodev_version=autodev_version,
        timestamp=datetime.now(timezone.utc).isoformat(),
        instances=outcomes,
        passed=passed,
        failed=failed,
        errored=errored,
        blind_count=blind_count,
        clean_count=clean_count,
        total_wall_time_s=sum(o.wall_time_s for o in outcomes),
        total_quota_wait_time_s=sum(o.quota_wait_time_s for o in outcomes),
        gate_verdict=gate_report.verdict,
        gate_status=gate_report.status,
        gate_reasons=list(gate_report.reasons),
        baseline_established=gate_report.baseline_established,
        baseline_path=(
            gate_report.baseline_path if gate_report.baseline_established else None
        ),
        recommend_lock=clean_count >= GO_NOGO_MIN_CLEAN,
        quota_wait_events=[asdict(e) for e in quota_wait_events],
    )


def write_pilot_report(
    report: PilotReport, out_dir: Path
) -> tuple[Path, Path]:
    """Write ``report`` to ``out_dir`` as ``pilot-report.json`` + ``pilot-summary.md``.

    Returns ``(json_path, summary_path)``. Creates ``out_dir`` if needed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "pilot-report.json"
    summary_path = out_dir / "pilot-summary.md"
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2), encoding="utf-8"
    )
    summary_path.write_text(report.human_summary(), encoding="utf-8")
    return json_path, summary_path


# ---------------------------------------------------------------------------
# Operator CLI (heavy imports are lazy — mirrors benchmarks.runner.external)
# ---------------------------------------------------------------------------


def _load_pipeline(
    args: argparse.Namespace,
) -> tuple[BenchmarkAdapter, list[Instance], SolveInvoker, Scorer]:
    """Lazily construct the concrete SWE-bench-Lite adapter/dataset/scorer.

    Imported inside the function (never at module load) so ``import
    benchmarks.runner.pilot`` stays hermetic — the ``datasets`` / ``sb-cli`` heavy
    paths are only touched on a real operator run.
    """
    import importlib

    adapters_mod = importlib.import_module("benchmarks.adapters.swebench_lite")
    dataset_mod = importlib.import_module("benchmarks.datasets.swebench_lite")
    scorer_mod = importlib.import_module("benchmarks.scorers.sbcli")

    adapter: BenchmarkAdapter = adapters_mod.build_adapter(args)
    all_instances: list[Instance] = list(dataset_mod.load_instances(args))
    selected = select_candidate_instances(all_instances, count=int(args.count))
    scorer: Scorer = scorer_mod.build_scorer(args)
    return adapter, list(selected), default_solve_invoker, scorer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.runner.pilot",
        description=(
            "Phase-1 SWE-bench-Lite pilot: screen candidate instances on the host "
            "(arm64) under the quota guard, score via sb-cli, and — on a healthy "
            "run — establish the first coarse-gate baseline. Measures throughput "
            "(wall-time + quota-wait) and which instances solve blind."
        ),
    )
    parser.add_argument("--dataset", default="swe-bench-lite", help="Dataset id.")
    parser.add_argument(
        "--instances", default=None, help="Instance selector (ids or a JSONL file)."
    )
    parser.add_argument(
        "--instances-file", default=None, help="Local instances JSONL (offline)."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_CANDIDATE_COUNT,
        help="How many candidate instances to screen (default ~30).",
    )
    parser.add_argument(
        "--workdir-root", type=Path, required=True, help="Root for per-instance workdirs."
    )
    parser.add_argument(
        "--out-dir", type=Path, required=True, help="Where to write the pilot report."
    )
    parser.add_argument("--run-id", default=None, help="Run id (default: timestamp).")
    parser.add_argument(
        "--autodev-version",
        default=None,
        help="Version stamped on the baseline (default: detected).",
    )
    parser.add_argument(
        "--baselines-root",
        type=Path,
        default=DEFAULT_BASELINES_ROOT,
        help="Root for the stored gate baselines.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Per-instance quota retry cap.",
    )
    parser.add_argument(
        "--swebench-timeout",
        type=int,
        default=None,
        help=(
            "Per-autodev-command (init/plan/execute) wall-clock timeout in seconds "
            "for the SWE-bench-Lite adapter. Default: the adapter's built-in "
            "DEFAULT_SWEBENCH_TIMEOUT when omitted (benchmarks.adapters.swebench_lite)."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator path
    parser = _build_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("pilot-%Y%m%dT%H%M%SZ")
    autodev_version = args.autodev_version or current_autodev_version()

    adapter, instances, invoker, scorer = _load_pipeline(args)
    if not instances:
        print("no candidate instances selected — check --instances / --dataset", file=sys.stderr)
        return 2

    report = run_pilot(
        adapter,
        scorer,
        instances,
        invoker,
        workdir_root=args.workdir_root,
        run_id=run_id,
        autodev_version=autodev_version,
        baselines_root=args.baselines_root,
        max_attempts=args.max_attempts,
    )
    json_path, summary_path = write_pilot_report(report, args.out_dir)
    print(report.human_summary())
    print(f"\nwrote {json_path}\nwrote {summary_path}")
    if report.baseline_established and report.baseline_path:
        print(f"established baseline: {report.baseline_path}")
    # Non-zero on a RED gate so a scripted run surfaces a degenerate/regressed slice.
    return 0 if report.gate_verdict == "green" else 1


__all__ = [
    "DEFAULT_CANDIDATE_COUNT",
    "GO_NOGO_MIN_CLEAN",
    "HEAVY_DEP_REPO_HINTS",
    "HEAVY_LITE_REPOS",
    "PilotInstanceOutcome",
    "PilotReport",
    "main",
    "pilot_instance_status",
    "run_pilot",
    "select_candidate_instances",
    "write_pilot_report",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
