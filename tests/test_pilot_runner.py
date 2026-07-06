"""Gate tests for the Phase-1 pilot runner (P1.6).

The pilot is the FIRST operational deliverable: it drives the whole Phase-1
pipeline over a candidate SWE-bench-Lite slice — host-arm64 adapter solve THROUGH
the quota guard (P1.4) → sb-cli scorer (P1.3) → coarse gate / baseline-establish
(P1.5) — and records, per instance, a run/skip/ERROR verdict plus wall-time,
quota-wait time and blind-vs-self-repair. The actual quota-consuming sweep is
operator-gated; this suite pins the pilot's *logic* fully hermetically (mocked
adapter / scorer / quota-guard, no network, no autodev, no sleep).

The four required proofs from the task:

  1. ``benchmarks.runner.pilot`` imports (no heavy/network import at module load);
  2. the per-instance status mapping is **ERROR-until-complete** — a quota wait /
     quota-exhausted instance is ERROR, NEVER a false FAIL;
  3. the candidate-selection helper returns a **non-empty, de-duplicated** list
     from a fixture dataset (and prefers lighter-dependency / pure-python repos);
  4. a mocked full run produces a report with per-instance status + wall-time and
     (on a healthy run) writes the first baseline via the gate.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from benchmarks.gate.coarse_gate import baseline_path_for
from benchmarks.datasets.swebench_lite import load_instances_from_jsonl
from benchmarks.runner.quota_guard import COMPLETE, GuardResult, run_guarded_solve
from benchmarks.runner.solve import SolveOutcome, SolveProfile
from benchmarks.scorers.base import ERROR, FAIL, PASS, InstanceScore, ScoreReport

import benchmarks.runner.pilot as pilot
from benchmarks.runner.pilot import (
    PilotInstanceOutcome,
    PilotReport,
    pilot_instance_status,
    run_pilot,
    select_candidate_instances,
    write_pilot_report,
)


# ---------------------------------------------------------------------------
# Builders for the mocked pipeline
# ---------------------------------------------------------------------------


def _outcome(*, wall_time_s: float, diff: str = "patch") -> SolveOutcome:
    return SolveOutcome(
        diff=diff,
        base_sha="basesha",
        success=True,
        empty_diff=not (diff and diff.strip()),
        diff_source="commit" if diff else "none",
        ledger_path=Path("ledger.jsonl"),
        failed_reason=None,
        calls=[],
        invocations=3,
        wall_time_s=wall_time_s,
        fail_stdout_tail="",
        fail_stderr_tail="",
    )


def _guard_complete(instance_id: str, wall_time_s: float) -> GuardResult:
    return GuardResult(
        instance_id=instance_id,
        status=COMPLETE,
        outcome=_outcome(wall_time_s=wall_time_s),
        attempts=1,
        quota_waits=0,
        quota_wait_time_s=0.0,
    )


def _guard_quota_exhausted(instance_id: str, quota_wait_time_s: float) -> GuardResult:
    return GuardResult(
        instance_id=instance_id,
        status=ERROR,
        outcome=None,
        attempts=6,
        quota_waits=5,
        quota_wait_time_s=quota_wait_time_s,
        detail="quota-exhausted after 6 attempts",
        quota_exhausted=True,
    )


class _Report:
    """Minimal InstanceReport double (only the fields the pilot reads for blind)."""

    def __init__(self, instance_id: str, degraded_blind: bool) -> None:
        self.instance_id = instance_id
        self.degraded_blind = degraded_blind


class _FakeAdapter:
    model_name = "autodev"
    name = "fake-adapter"

    def __init__(self, reports: list[_Report]) -> None:
        self.reports = reports


class _FakeScorer:
    """Returns a pre-scripted ScoreReport, echoing the run_id it was handed."""

    name = "fake-scorer"

    def __init__(self, verdicts: dict[str, str]) -> None:
        self._verdicts = verdicts
        self.seen_run_id: str | None = None
        self.seen_predictions: list | None = None

    def score(self, predictions, *, run_id: str) -> ScoreReport:
        self.seen_run_id = run_id
        self.seen_predictions = list(predictions)
        return ScoreReport(
            instances=[
                InstanceScore(iid, status) for iid, status in self._verdicts.items()
            ],
            summary={"run_id": run_id},
        )


def _fake_guarded_solve_factory(predictions: list[dict], guards: list[GuardResult]):
    """Build a stand-in for ``run_guarded_solve`` returning scripted results.

    Accepts the exact kwargs ``run_pilot`` passes so a signature drift fails
    loudly rather than silently ignoring the quota-guard wiring.
    """

    def fake(adapter, instances, invoker, *, workdir_root, max_attempts, backoff,
             sleep, on_quota_wait):
        fake.seen = {  # type: ignore[attr-defined]
            "workdir_root": workdir_root,
            "max_attempts": max_attempts,
        }
        return list(predictions), list(guards)

    return fake


# ---------------------------------------------------------------------------
# 1. import
# ---------------------------------------------------------------------------


def test_pilot_module_imports():
    assert hasattr(pilot, "run_pilot")
    assert hasattr(pilot, "select_candidate_instances")


# ---------------------------------------------------------------------------
# 2. ERROR-until-complete status mapping (a quota wait never becomes FAIL)
# ---------------------------------------------------------------------------


def test_pilot_status_complete_passes_through_verdict():
    """A COMPLETE guard outcome keeps the scorer's real capability verdict."""
    assert pilot_instance_status(_guard_complete("i", 1.0), PASS) == PASS
    assert pilot_instance_status(_guard_complete("i", 1.0), FAIL) == FAIL
    assert pilot_instance_status(_guard_complete("i", 1.0), ERROR) == ERROR


def test_pilot_status_quota_exhausted_is_error_never_fail():
    """THE invariant: a quota-exhausted instance is ERROR — even if a (hypothetical)
    scorer verdict said FAIL, the pilot must NOT record it as a capability FAIL.

    Non-vacuous: a naive ``return score_status`` would yield FAIL here; this pins
    ERROR-until-complete.
    """
    g = _guard_quota_exhausted("q", quota_wait_time_s=900.0)
    assert pilot_instance_status(g, FAIL) == ERROR
    assert pilot_instance_status(g, ERROR) == ERROR
    assert pilot_instance_status(g, PASS) == ERROR  # in-flight never a real PASS


def test_run_pilot_quota_exhausted_surfaces_error_not_fail(tmp_path: Path):
    """End-to-end through run_pilot: a quota-exhausted instance surfaces as ERROR,
    never FAIL, even if the scorer somehow returned FAIL for it."""
    guards = [_guard_quota_exhausted("q1", 600.0)]
    preds = [{"instance_id": "q1", "model_name_or_path": "autodev", "model_patch": ""}]
    adapter = _FakeAdapter(reports=[])
    # A hostile scorer that (wrongly) calls the quota-exhausted instance FAIL.
    scorer = _FakeScorer({"q1": FAIL})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "q1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid",
        autodev_version="9.9.9-test",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )
    (only,) = report.instances
    assert only.status == ERROR
    assert only.status != FAIL
    assert only.quota_exhausted is True
    assert only.quota_wait_time_s == 600.0


# ---------------------------------------------------------------------------
# 3. candidate selection — non-empty, de-duplicated, prefers pure-python
# ---------------------------------------------------------------------------


def _write_fixture_dataset(tmp_path: Path) -> Path:
    """A tiny JSONL dataset with a DUPLICATE id and a heavy-dep repo mixed in."""
    import json

    rows = [
        {"instance_id": "pallets__flask-1", "repo": "pallets/flask"},
        {"instance_id": "psf__requests-1", "repo": "psf/requests"},
        # duplicate of the first id — must be de-duplicated
        {"instance_id": "pallets__flask-1", "repo": "pallets/flask"},
        # a heavy native-build repo — deprioritised by the heuristic
        {"instance_id": "numpy__numpy-1", "repo": "numpy/numpy"},
        {"instance_id": "no-id-empty", "repo": ""},
    ]
    p = tmp_path / "instances.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_select_candidates_non_empty_and_deduplicated(tmp_path: Path):
    instances = load_instances_from_jsonl(_write_fixture_dataset(tmp_path))
    selected = select_candidate_instances(instances, count=30)

    ids = [s["instance_id"] for s in selected]
    assert selected, "candidate selection returned an empty list"
    assert len(ids) == len(set(ids)), f"ids not de-duplicated: {ids}"
    assert "pallets__flask-1" in ids
    # de-dup collapsed the two flask rows to one
    assert ids.count("pallets__flask-1") == 1


def test_select_candidates_prefers_pure_python_when_truncating(tmp_path: Path):
    """The documented arm64-friendliness heuristic: pure-python repos outrank
    heavy native-build repos, so a tight count drops the heavy one first."""
    instances = load_instances_from_jsonl(_write_fixture_dataset(tmp_path))
    selected = select_candidate_instances(instances, count=2)
    ids = [s["instance_id"] for s in selected]
    assert len(ids) == 2
    # numpy (heavy native build) is deprioritised out of the top-2
    assert "numpy__numpy-1" not in ids


def test_heavy_lite_repos_classified_heavy_even_without_depname_in_slug():
    """Regression for the Phase-1 smoke (all 3 picks were astropy, all blind):
    numpy/C-extension Lite repos whose SLUG doesn't contain a dep-name hint
    (astropy, seaborn, xarray) must still be heavy; pure-python Lite repos must
    stay in the friendly tier."""
    from benchmarks.runner.pilot import HEAVY_DEP_REPO_HINTS, _is_heavy_repo

    for repo in (
        "astropy/astropy",
        "mwaskom/seaborn",
        "pydata/xarray",
        "matplotlib/matplotlib",
        "scikit-learn/scikit-learn",
    ):
        assert _is_heavy_repo({"repo": repo}, HEAVY_DEP_REPO_HINTS) is True, repo
    for repo in (
        "pallets/flask",
        "psf/requests",
        "django/django",
        "sympy/sympy",
        "pytest-dev/pytest",
        "sphinx-doc/sphinx",
        "pylint-dev/pylint",
    ):
        assert _is_heavy_repo({"repo": repo}, HEAVY_DEP_REPO_HINTS) is False, repo


def test_select_candidates_deprioritizes_astropy_below_pure_python():
    """astropy sorts first alphabetically and was previously mis-classified light,
    so a tight count picked it; now a pure-python repo outranks it."""
    from benchmarks.runner.pilot import select_candidate_instances

    instances = [
        {"instance_id": "astropy__astropy-1", "repo": "astropy/astropy"},
        {"instance_id": "astropy__astropy-2", "repo": "astropy/astropy"},
        {"instance_id": "pallets__flask-1", "repo": "pallets/flask"},
    ]
    ids = [s["instance_id"] for s in select_candidate_instances(instances, count=1)]
    assert ids == ["pallets__flask-1"]


def test_select_candidates_round_robin_spreads_across_repos():
    """A tight count samples multiple friendly repos instead of exhausting the
    first (largest) one; heavy repos are still dropped first."""
    from benchmarks.runner.pilot import select_candidate_instances

    instances = (
        [
            {"instance_id": f"django__django-{i}", "repo": "django/django"}
            for i in range(5)
        ]
        + [
            {"instance_id": f"pallets__flask-{i}", "repo": "pallets/flask"}
            for i in range(5)
        ]
        + [{"instance_id": "astropy__astropy-1", "repo": "astropy/astropy"}]
    )
    selected = select_candidate_instances(instances, count=4)
    repos = [s["repo"] for s in selected]
    assert "astropy/astropy" not in repos  # heavy dropped first
    assert repos.count("django/django") == 2  # spread, not 4-of-django
    assert repos.count("pallets/flask") == 2
    assert repos[0] != repos[1]  # interleaved, not clustered


# ---------------------------------------------------------------------------
# 4. a mocked healthy full run → report w/ per-instance status+wall-time + baseline
# ---------------------------------------------------------------------------


def test_run_pilot_healthy_run_writes_baseline_and_report(tmp_path: Path):
    n = 14
    instances = [
        {"instance_id": f"inst-{i}", "problem_statement": "fix", "repo": "pallets/flask"}
        for i in range(n)
    ]
    # 10 PASS + 4 FAIL, no ERROR → completed=14 (>=min 12), error_rate=0 → healthy.
    verdicts = {f"inst-{i}": (PASS if i < 10 else FAIL) for i in range(n)}
    preds = [
        {"instance_id": f"inst-{i}", "model_name_or_path": "autodev", "model_patch": "p"}
        for i in range(n)
    ]
    guards = [_guard_complete(f"inst-{i}", wall_time_s=float(i + 1)) for i in range(n)]
    # first 3 solved blind (arm64 deps failed) — self-repair off
    reports = [_Report(f"inst-{i}", degraded_blind=(i < 3)) for i in range(n)]
    adapter = _FakeAdapter(reports=reports)
    scorer = _FakeScorer(verdicts)
    baselines_root = tmp_path / "baselines"

    report = run_pilot(
        adapter,
        scorer,
        instances=instances,
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="pilot-1",
        autodev_version="1.2.3-main-abc",
        baselines_root=baselines_root,
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )

    assert isinstance(report, PilotReport)
    # the scorer was actually invoked with the pilot's run_id + all predictions
    assert scorer.seen_run_id == "pilot-1"
    assert scorer.seen_predictions is not None and len(scorer.seen_predictions) == n

    # per-instance status + wall-time recorded for every instance
    assert len(report.instances) == n
    assert all(isinstance(o, PilotInstanceOutcome) for o in report.instances)
    assert all(o.wall_time_s > 0.0 for o in report.instances)
    by_id = {o.instance_id: o for o in report.instances}
    assert by_id["inst-0"].status == PASS
    assert by_id["inst-13"].status == FAIL
    # blind flag threaded through from the adapter's InstanceReports
    assert by_id["inst-0"].blind is True
    assert by_id["inst-9"].blind is False
    assert report.blind_count == 3
    assert report.passed == 10
    assert report.failed == 4
    assert report.errored == 0

    # healthy first run → baseline ESTABLISHED (gate green) at the versioned path
    assert report.gate_verdict == "green"
    assert report.baseline_established is True
    baseline = baseline_path_for(baselines_root, "1.2.3-main-abc")
    assert baseline.is_file(), f"baseline not written at {baseline}"

    # the report round-trips to disk (JSON + human summary)
    json_path, summary_path = write_pilot_report(report, tmp_path / "out")
    assert json_path.is_file() and summary_path.is_file()
    import json

    doc = json.loads(json_path.read_text(encoding="utf-8"))
    assert doc["run_id"] == "pilot-1"
    assert len(doc["instances"]) == n
    assert "inst-0" in summary_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. The quota guard is LOCKED onto the pilot's (field) solve path (anti
#    "field-inert" — a shipped feature that is only wired in tests).
# ---------------------------------------------------------------------------


def test_run_pilot_defaults_to_the_quota_guard():
    """Anti-field-inert lock: the pilot's per-instance solve path defaults to the
    REAL quota guard (``run_guarded_solve``) — the overnight operator entry point
    (:func:`pilot.main`) never overrides ``guarded_solve``, so this default IS the
    field path. If the pilot were rewired to a naive un-guarded loop, this default
    would change and this test would fail."""
    default = inspect.signature(run_pilot).parameters["guarded_solve"].default
    assert default is run_guarded_solve


def test_run_pilot_routes_solve_through_quota_guard_and_forces_serial(tmp_path: Path):
    """Integration lock: ``run_pilot`` actually drives each instance THROUGH
    ``run_guarded_solve``, which forces ``max_parallel_subprocesses = 1`` on every
    profile before solving. A spy delegates to the REAL guard (with a scripted
    ``solve_fn``) so the guard's serial enforcement is genuinely exercised from the
    pilot entry point — it can never silently regress to an un-guarded burst loop.

    Non-vacuous: the adapter deliberately requests burst parallelism (8); if the
    guard were bypassed the profiles reaching ``solve_fn`` would still say 8."""
    seen_profiles: list[SolveProfile] = []
    invoked_over: dict[str, int] = {}

    class _BurstAdapter:
        name = "burst"
        model_name = "autodev"

        def __init__(self) -> None:
            self.reports: list = []

        def prepare(self, instance, workdir: Path) -> SolveProfile:
            # Request burst parallelism so the guard MUST override it to serial.
            return SolveProfile(
                config_patch={"tournaments": {"max_parallel_subprocesses": 8}}
            )

        def intent(self, instance) -> str:
            return str(instance["problem_statement"])

        def predict(self, instance, workdir: Path, outcome: SolveOutcome):
            return {
                "instance_id": str(instance["instance_id"]),
                "model_name_or_path": self.model_name,
                "model_patch": outcome.diff,
            }

    def scripted_solve_fn(workdir, intent, profile, invoker):
        seen_profiles.append(profile)
        return _outcome(wall_time_s=1.0, diff="patch")

    def spy_guarded_solve(adapter, instances, invoker, **kwargs):
        insts = list(instances)
        invoked_over["count"] = len(insts)
        # Delegate to the REAL guard with a scripted solve_fn so the guard's
        # serial enforcement is actually exercised on the pilot's path.
        return run_guarded_solve(
            adapter, insts, invoker, solve_fn=scripted_solve_fn, **kwargs
        )

    instances = [
        {"instance_id": f"g-{i}", "problem_statement": "fix", "repo": "pallets/flask"}
        for i in range(3)
    ]
    scorer = _FakeScorer({f"g-{i}": PASS for i in range(3)})

    run_pilot(
        _BurstAdapter(),
        scorer,
        instances=instances,
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid",
        autodev_version="9.9.9-guard",
        baselines_root=tmp_path / "b",
        guarded_solve=spy_guarded_solve,
    )

    # the guard was invoked over EVERY instance...
    assert invoked_over["count"] == 3
    # ...and every profile the guard handed to solve was forced serial (8 -> 1).
    assert len(seen_profiles) == 3, "guard never reached the solve_fn for each instance"
    assert all(
        p.config_patch["tournaments"]["max_parallel_subprocesses"] == 1
        for p in seen_profiles
    )


def test_run_pilot_degenerate_run_does_not_write_baseline(tmp_path: Path):
    """Anti-vacuity carries through the pilot: a too-small / ERROR-heavy slice is
    RED and NO baseline is written (a degenerate baseline would poison the gate)."""
    n = 4  # < min_completed (12)
    instances = [
        {"instance_id": f"d-{i}", "problem_statement": "fix", "repo": "pallets/flask"}
        for i in range(n)
    ]
    verdicts = {f"d-{i}": PASS for i in range(n)}
    preds = [
        {"instance_id": f"d-{i}", "model_name_or_path": "autodev", "model_patch": "p"}
        for i in range(n)
    ]
    guards = [_guard_complete(f"d-{i}", wall_time_s=1.0) for i in range(n)]
    adapter = _FakeAdapter(reports=[_Report(f"d-{i}", False) for i in range(n)])
    scorer = _FakeScorer(verdicts)
    baselines_root = tmp_path / "baselines"

    report = run_pilot(
        adapter,
        scorer,
        instances=instances,
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="pilot-degenerate",
        autodev_version="0.0.0-degen",
        baselines_root=baselines_root,
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )

    assert report.gate_verdict == "red"
    assert report.baseline_established is False
    assert not baseline_path_for(baselines_root, "0.0.0-degen").exists()
