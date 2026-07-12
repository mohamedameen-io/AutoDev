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
import re
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


def _outcome(
    *,
    wall_time_s: float,
    diff: str = "patch",
    ledger_path: Path = Path("ledger.jsonl"),
) -> SolveOutcome:
    return SolveOutcome(
        diff=diff,
        base_sha="basesha",
        success=True,
        empty_diff=not (diff and diff.strip()),
        diff_source="commit" if diff else "none",
        ledger_path=ledger_path,
        failed_reason=None,
        calls=[],
        invocations=3,
        wall_time_s=wall_time_s,
        fail_stdout_tail="",
        fail_stderr_tail="",
    )


def _guard_complete(
    instance_id: str,
    wall_time_s: float,
    *,
    detail: str | None = None,
    fail_stdout_tail: str = "",
    fail_stderr_tail: str = "",
    ledger_path: Path = Path("ledger.jsonl"),
) -> GuardResult:
    return GuardResult(
        instance_id=instance_id,
        status=COMPLETE,
        outcome=_outcome(wall_time_s=wall_time_s, ledger_path=ledger_path),
        attempts=1,
        quota_waits=0,
        quota_wait_time_s=0.0,
        detail=detail,
        fail_stdout_tail=fail_stdout_tail,
        fail_stderr_tail=fail_stderr_tail,
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
    """Minimal InstanceReport double (only the fields the pilot reads for blind
    + the install-failure tails)."""

    def __init__(
        self,
        instance_id: str,
        degraded_blind: bool,
        *,
        install_stdout_tail: str = "",
        install_stderr_tail: str = "",
    ) -> None:
        self.instance_id = instance_id
        self.degraded_blind = degraded_blind
        self.install_stdout_tail = install_stdout_tail
        self.install_stderr_tail = install_stderr_tail


class _FakeAdapter:
    model_name = "autodev"
    name = "fake-adapter"

    def __init__(self, reports: list[_Report]) -> None:
        self.reports = reports


class _FakeScorer:
    """Returns a pre-scripted ScoreReport, echoing the run_id it was handed.

    ``details`` optionally supplies a per-instance ``InstanceScore.detail`` (the
    SCORE-side diagnostic, e.g. WS-1's "sb-cli eval did not complete (infra)")
    -- absent by default (``None``) so every existing caller that only passes
    ``verdicts`` is unaffected.
    """

    name = "fake-scorer"

    def __init__(
        self, verdicts: dict[str, str], details: dict[str, str] | None = None
    ) -> None:
        self._verdicts = verdicts
        self._details = details or {}
        self.seen_run_id: str | None = None
        self.seen_predictions: list | None = None

    def score(self, predictions, *, run_id: str) -> ScoreReport:
        self.seen_run_id = run_id
        self.seen_predictions = list(predictions)
        return ScoreReport(
            instances=[
                InstanceScore(iid, status, self._details.get(iid))
                for iid, status in self._verdicts.items()
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
# 4b. WS-1: the scorer's per-instance InstanceScore.detail threads onto
#     PilotInstanceOutcome.score_detail, and per-instance cost_usd is read
#     from each workdir's run-summary.jsonl (sibling of outcome.ledger_path).
# ---------------------------------------------------------------------------


def test_run_pilot_threads_score_detail_from_scorer_into_outcome_and_json(
    tmp_path: Path,
):
    """InstanceScore.detail (the SCORE-side diagnostic — e.g. WS-1's "sb-cli
    eval did not complete (infra)") must be threaded onto
    PilotInstanceOutcome.score_detail -- a field DISTINCT from the SOLVE-side
    ``detail`` (the quota-guard's own detail) -- and survive the JSON
    round-trip + appear in the human summary, so a scoring-side infra failure
    is diagnosable from the pilot report alone."""
    import json

    guards = [_guard_complete("s1", 1.0)]
    preds = [{"instance_id": "s1", "model_name_or_path": "autodev", "model_patch": "p"}]
    adapter = _FakeAdapter(reports=[])
    scorer = _FakeScorer(
        {"s1": ERROR}, details={"s1": "sb-cli eval did not complete (infra)"}
    )

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "s1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-score-detail",
        autodev_version="9.9.9-score-detail",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )
    (only,) = report.instances
    assert only.score_detail == "sb-cli eval did not complete (infra)"
    # the solve-side `detail` (None here -- a clean COMPLETE guard result) is a
    # genuinely distinct field, never conflated with score_detail.
    assert only.detail is None

    json_path, _ = write_pilot_report(report, tmp_path / "out")
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    (inst_doc,) = doc["instances"]
    assert inst_doc["score_detail"] == "sb-cli eval did not complete (infra)"

    summary = report.human_summary()
    assert "score_detail: sb-cli eval did not complete (infra)" in summary


def test_human_summary_does_not_surface_score_detail_for_passing_instance():
    """I-2 regression: the REAL SbcliScorer sets a non-empty ``detail`` on EVERY
    verdict -- including ``detail="resolved"`` for a PASS (see sbcli.py). If that
    is threaded onto ``score_detail`` and surfaced unconditionally, every healthy
    pass gets listed under "## Failure detail" as ``- score_detail: resolved``,
    defeating the section on a green run.

    RED before the fix: the pre-fix ``or o.score_detail`` filter clause + the
    unconditional ``if o.score_detail`` render pulled the PASS into the section.
    GREEN after gating the surfacing on a non-PASS status.

    Non-vacuous: a FAIL carrying ``score_detail="unresolved"`` DOES appear in the
    section, proving the guard keys on STATUS, not on score_detail being empty.
    The PASS's score_detail is still kept in the JSON (to_dict); only the
    human-summary surfacing is suppressed."""
    passing = PilotInstanceOutcome(
        instance_id="pass-1",
        status=PASS,
        wall_time_s=2.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=False,
        quota_exhausted=False,
        detail=None,
        score_detail="resolved",  # exactly what the real SbcliScorer sets on PASS
    )
    failing = PilotInstanceOutcome(
        instance_id="fail-1",
        status=FAIL,
        wall_time_s=3.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=False,
        quota_exhausted=False,
        detail=None,
        score_detail="unresolved",
    )
    report = PilotReport(
        run_id="rid-score-pass",
        autodev_version="1.0.0",
        timestamp="2026-01-01T00:00:00+00:00",
        instances=[passing, failing],
        passed=1,
        failed=1,
        errored=0,
        blind_count=0,
        clean_count=2,
        total_wall_time_s=5.0,
        total_quota_wait_time_s=0.0,
        gate_verdict="green",
        gate_status="ok",
        gate_reasons=[],
        baseline_established=True,
        baseline_path=None,
        recommend_lock=False,
    )

    summary = report.human_summary()
    assert "## Failure detail" in summary
    detail_section = summary.split("## Failure detail", 1)[1]
    # the PASS is entirely absent from the failure-detail section ...
    assert "pass-1" not in detail_section
    assert "score_detail: resolved" not in detail_section
    # ... while the FAIL's score_detail IS surfaced (non-vacuous control).
    assert "fail-1" in detail_section
    assert "score_detail: unresolved" in detail_section

    # The PASS's score_detail is still preserved in the machine-readable JSON.
    doc = report.to_dict()
    by_id = {i["instance_id"]: i for i in doc["instances"]}
    assert by_id["pass-1"]["score_detail"] == "resolved"


def test_run_pilot_reads_cost_usd_from_run_summary_sibling_of_ledger_path(
    tmp_path: Path,
):
    """cost_usd is summed from every line of the instance's run-summary.jsonl --
    the SIBLING of the terminal SolveOutcome.ledger_path (both live under the
    solved workdir's .autodev/ directory; see src/state/run_summary.py) -- and
    must survive the JSON round-trip and appear in the per-instance table."""
    import json

    autodev_dir = tmp_path / "instance-workdir" / ".autodev"
    autodev_dir.mkdir(parents=True)
    ledger_path = autodev_dir / "plan-ledger.jsonl"
    ledger_path.write_text("", encoding="utf-8")
    run_summary_rows = [
        {"phase": "plan", "cost_usd": 0.5, "elapsed_s": 1.0, "tasks": 1,
         "ts": "2026-01-01T00:00:00+00:00"},
        {"phase": "execute", "cost_usd": 1.25, "elapsed_s": 2.0, "tasks": 1,
         "ts": "2026-01-01T00:00:01+00:00"},
    ]
    (autodev_dir / "run-summary.jsonl").write_text(
        "\n".join(json.dumps(r) for r in run_summary_rows) + "\n", encoding="utf-8"
    )

    guards = [_guard_complete("c1", 1.0, ledger_path=ledger_path)]
    preds = [{"instance_id": "c1", "model_name_or_path": "autodev", "model_patch": "p"}]
    adapter = _FakeAdapter(reports=[])
    scorer = _FakeScorer({"c1": PASS})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "c1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-cost",
        autodev_version="9.9.9-cost",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )
    (only,) = report.instances
    assert only.cost_usd == 1.75  # 0.5 + 1.25, both exact in binary float

    json_path, _ = write_pilot_report(report, tmp_path / "out")
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    (inst_doc,) = doc["instances"]
    assert inst_doc["cost_usd"] == 1.75

    summary = report.human_summary()
    assert "1.7500" in summary  # rendered in the per-instance table


def test_run_pilot_cost_usd_defaults_to_zero_when_no_run_summary_present(
    tmp_path: Path,
):
    """Non-vacuous control: an instance whose workdir has NO run-summary.jsonl
    (e.g. a fresh/never-written workdir) must default cost_usd to 0.0 -- never
    crash, never fabricate spend that was not actually recorded."""
    guards = [_guard_complete("z1", 1.0)]
    preds = [{"instance_id": "z1", "model_name_or_path": "autodev", "model_patch": "p"}]
    adapter = _FakeAdapter(reports=[])
    scorer = _FakeScorer({"z1": PASS})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "z1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-cost-zero",
        autodev_version="9.9.9-cost-zero",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )
    (only,) = report.instances
    assert only.cost_usd == 0.0


def test_run_pilot_cost_usd_is_zero_when_outcome_is_none(tmp_path: Path):
    """A guard result with outcome=None (e.g. quota-exhausted -- every attempt
    raised without ever returning a SolveOutcome) has no ledger_path to read a
    run-summary.jsonl from; cost_usd must default to 0.0, never crash."""
    guards = [_guard_quota_exhausted("q1", 600.0)]
    preds = [{"instance_id": "q1", "model_name_or_path": "autodev", "model_patch": ""}]
    adapter = _FakeAdapter(reports=[])
    scorer = _FakeScorer({"q1": FAIL})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "q1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-cost-none",
        autodev_version="9.9.9-cost-none",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )
    (only,) = report.instances
    assert only.cost_usd == 0.0


def test_instance_cost_usd_helper_sums_and_defaults_safely(tmp_path: Path):
    """Direct unit coverage of _instance_cost_usd: a None ledger_path and a
    missing run-summary.jsonl both default to 0.0; a well-formed file sums
    cost_usd across every line; a malformed line is skipped, never fatal; AND
    (I-1 regression) a line that is VALID JSON but not an object -- ``null``, a
    bare scalar, an array (exactly what a truncated/interleaved concurrent write
    produces) -- is skipped, contributes 0.0, and NEVER raises (a raise here
    would escape the unguarded run_pilot loop and abort the whole pilot).

    RED before the fix: ``json.loads("null")`` returns ``None`` and
    ``None.get("cost_usd")`` raises ``AttributeError`` -- which the old
    ``except (TypeError, ValueError)`` did NOT catch, so this test raised
    instead of returning a float."""
    from benchmarks.runner.pilot import _instance_cost_usd

    assert _instance_cost_usd(None) == 0.0

    missing_dir = tmp_path / "no-such-workdir" / ".autodev"
    missing_dir.mkdir(parents=True)
    assert _instance_cost_usd(missing_dir / "plan-ledger.jsonl") == 0.0

    autodev_dir = tmp_path / "real-workdir" / ".autodev"
    autodev_dir.mkdir(parents=True)
    (autodev_dir / "run-summary.jsonl").write_text(
        "\n".join(
            [
                '{"phase": "plan", "cost_usd": 0.25}',
                "not json at all",  # unparseable line -- must not raise
                "null",  # valid JSON, NOT an object -- must not raise (I-1)
                "42",  # valid JSON bare scalar -- must not raise (I-1)
                '["cost_usd", 0.75]',  # valid JSON array -- must not raise (I-1)
                '{"phase": "execute", "cost_usd": 0.75}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # only the two real object rows contribute; every non-object line is 0.0.
    assert _instance_cost_usd(autodev_dir / "plan-ledger.jsonl") == 1.0

    # And a file whose ONLY content is valid-JSON-non-object lines is a clean
    # 0.0, not a crash (the pure I-1 case with no valid rows to mask a raise).
    only_non_objects = tmp_path / "non-object-only" / ".autodev"
    only_non_objects.mkdir(parents=True)
    (only_non_objects / "run-summary.jsonl").write_text(
        "null\n42\n[1, 2, 3]\n", encoding="utf-8"
    )
    assert _instance_cost_usd(only_non_objects / "plan-ledger.jsonl") == 0.0


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


# ---------------------------------------------------------------------------
# 6. CLI: --swebench-timeout is accepted and None-by-default (adapter fallback)
# ---------------------------------------------------------------------------


def test_build_parser_accepts_swebench_timeout_and_defaults_to_none():
    """``--swebench-timeout`` is a new, purely-additive flag: omitted, it parses
    to ``None`` -- the load-bearing default that keeps ``build_adapter``'s
    ``getattr(args, "swebench_timeout", None) or DEFAULT_SWEBENCH_TIMEOUT``
    fallback (``benchmarks.adapters.swebench_lite``) byte-for-byte unchanged.
    Supplied, it threads the operator's override straight onto ``args``."""
    parser = pilot._build_parser()
    required = ["--workdir-root", "/tmp/wd", "--out-dir", "/tmp/out"]

    default_args = parser.parse_args(required)
    assert default_args.swebench_timeout is None

    overridden = parser.parse_args([*required, "--swebench-timeout", "7200"])
    assert overridden.swebench_timeout == 7200


# ---------------------------------------------------------------------------
# 7. fail_stdout_tail/fail_stderr_tail surfaced end-to-end (Piece 4): a
#    timeout/error must be diagnosable from the pilot report alone, with no
#    need to hand-inspect the instance's workdir.
# ---------------------------------------------------------------------------


def test_run_pilot_threads_fail_tails_from_guard_result_into_outcome_and_json(
    tmp_path: Path,
):
    """fail_stdout_tail/fail_stderr_tail on a GuardResult must be threaded onto
    the resulting PilotInstanceOutcome AND survive the JSON round-trip (through
    PilotReport.to_dict()'s asdict()) — the whole point of the fix: a future
    timeout/error is diagnosable from pilot-report.json alone."""
    import json

    guards = [
        _guard_complete(
            "t1",
            1.0,
            detail="autodev execute timed out after 1800s",
            fail_stdout_tail="stdout tail content",
            fail_stderr_tail="stderr tail content",
        )
    ]
    preds = [{"instance_id": "t1", "model_name_or_path": "autodev", "model_patch": ""}]
    adapter = _FakeAdapter(reports=[])
    scorer = _FakeScorer({"t1": ERROR})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "t1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-tails",
        autodev_version="9.9.9-tails",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )

    (only,) = report.instances
    assert only.fail_stdout_tail == "stdout tail content"
    assert only.fail_stderr_tail == "stderr tail content"

    json_path, _ = write_pilot_report(report, tmp_path / "out")
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    (inst_doc,) = doc["instances"]
    assert inst_doc["fail_stdout_tail"] == "stdout tail content"
    assert inst_doc["fail_stderr_tail"] == "stderr tail content"


def test_run_pilot_threads_install_tails_from_adapter_reports_into_outcome_and_json(
    tmp_path: Path,
):
    """install_stdout_tail/install_stderr_tail on the adapter's InstanceReport
    (the per-instance arm64-install-failure capture, WS-7) must be threaded onto
    the resulting PilotInstanceOutcome -- mirroring exactly how degraded_blind
    already flows through ``_blind_map`` -- AND survive the JSON round-trip, so
    a blind instance's install failure is diagnosable from pilot-report.json
    alone, without hand-inspecting the instance's workdir / .autodev-bench log."""
    import json

    guards = [_guard_complete("i1", 1.0)]
    preds = [{"instance_id": "i1", "model_name_or_path": "autodev", "model_patch": ""}]
    adapter = _FakeAdapter(
        reports=[
            _Report(
                "i1",
                degraded_blind=True,
                install_stdout_tail="venv stdout tail",
                install_stderr_tail="pip stderr tail",
            )
        ]
    )
    scorer = _FakeScorer({"i1": ERROR})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "i1", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-install-tails",
        autodev_version="9.9.9-install-tails",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )

    (only,) = report.instances
    assert only.install_stdout_tail == "venv stdout tail"
    assert only.install_stderr_tail == "pip stderr tail"

    json_path, _ = write_pilot_report(report, tmp_path / "out")
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    (inst_doc,) = doc["instances"]
    assert inst_doc["install_stdout_tail"] == "venv stdout tail"
    assert inst_doc["install_stderr_tail"] == "pip stderr tail"


def test_run_pilot_install_tails_default_empty_when_adapter_report_lacks_them(
    tmp_path: Path,
):
    """Non-vacuous control: an adapter InstanceReport double that does NOT set
    the install tails at all (the ``_Report`` default, matching every OTHER
    existing fixture in this suite) must default to empty strings on the
    PilotInstanceOutcome -- never crash, never leak a stale/garbage value."""
    guards = [_guard_complete("i2", 1.0)]
    preds = [{"instance_id": "i2", "model_name_or_path": "autodev", "model_patch": ""}]
    adapter = _FakeAdapter(reports=[_Report("i2", degraded_blind=False)])
    scorer = _FakeScorer({"i2": PASS})

    report = run_pilot(
        adapter,
        scorer,
        instances=[{"instance_id": "i2", "problem_statement": "x", "repo": "a/b"}],
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        run_id="rid-install-tails-empty",
        autodev_version="9.9.9-install-tails-empty",
        baselines_root=tmp_path / "baselines",
        guarded_solve=_fake_guarded_solve_factory(preds, guards),
    )

    (only,) = report.instances
    assert only.install_stdout_tail == ""
    assert only.install_stderr_tail == ""


def test_human_summary_surfaces_install_tails_for_blind_instance():
    """WS7 #3 (red-before-green): a blind instance whose ONLY diagnostic is the
    arm64 install failure — ``install_stdout_tail``/``install_stderr_tail``
    populated, but ``detail`` and BOTH solve-fail tails empty — must still be
    surfaced in the human summary's failure-detail section, with the install
    tails rendered under DISTINCT ``install stdout``/``install stderr`` labels
    (a different pipeline stage than the solve-fail ``stdout``/``stderr``).

    RED before the fix: the pre-fix ``reportable`` filter keys only on
    ``detail``/``fail_*_tail``, so this instance is dropped from the section
    entirely. GREEN after the filter + render widening."""
    blind = PilotInstanceOutcome(
        instance_id="blind-1",
        status=ERROR,
        wall_time_s=3.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=True,
        quota_exhausted=False,
        detail=None,
        fail_stdout_tail="",
        fail_stderr_tail="",
        install_stdout_tail="uv venv created ok; then pip install -e . failed",
        install_stderr_tail="error: could not build wheels for native-ext",
    )
    report = PilotReport(
        run_id="rid-install",
        autodev_version="1.0.0",
        timestamp="2026-01-01T00:00:00+00:00",
        instances=[blind],
        passed=0,
        failed=0,
        errored=1,
        blind_count=1,
        clean_count=0,
        total_wall_time_s=3.0,
        total_quota_wait_time_s=0.0,
        gate_verdict="red",
        gate_status="insufficient",
        gate_reasons=[],
        baseline_established=False,
        baseline_path=None,
        recommend_lock=False,
    )

    summary = report.human_summary()

    # The blind instance appears in the failure-detail section (not just the
    # per-instance table) ...
    assert "## Failure detail" in summary
    detail_section = summary.split("## Failure detail", 1)[1]
    assert "blind-1" in detail_section
    # ... with DISTINCT install-stage labels (NOT the solve-fail stdout/stderr) ...
    assert "install stdout" in detail_section
    assert "install stderr" in detail_section
    # ... and the actual captured install failure text rendered in the block.
    assert "could not build wheels for native-ext" in detail_section


def test_human_summary_renders_failure_detail_with_excerpt_and_json_pointer():
    """human_summary() must surface a NEW failure-detail section — it does NOT
    surface ``detail`` at all today, only the JSON does — with: the failing
    instance's id, a TRUNCATED excerpt of a long tail, and a pointer to
    pilot-report.json for the full tail. Negative control: a clean/passing
    instance with no detail/tails must NOT produce any failure-detail content
    for itself."""
    long_tail = "x" * 50 + "MIDDLE_MARKER" + "y" * 400  # 463 chars, > 300
    failing = PilotInstanceOutcome(
        instance_id="fail-1",
        status=ERROR,
        wall_time_s=5.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=False,
        quota_exhausted=False,
        detail="autodev execute timed out after 1800s",
        fail_stdout_tail=long_tail,
        fail_stderr_tail="short stderr",
    )
    clean = PilotInstanceOutcome(
        instance_id="clean-1",
        status=PASS,
        wall_time_s=2.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=False,
        quota_exhausted=False,
        detail=None,
        fail_stdout_tail="",
        fail_stderr_tail="",
    )
    report = PilotReport(
        run_id="rid",
        autodev_version="1.0.0",
        timestamp="2026-01-01T00:00:00+00:00",
        instances=[failing, clean],
        passed=1,
        failed=0,
        errored=1,
        blind_count=0,
        clean_count=0,
        total_wall_time_s=7.0,
        total_quota_wait_time_s=0.0,
        gate_verdict="red",
        gate_status="insufficient",
        gate_reasons=[],
        baseline_established=False,
        baseline_path=None,
        recommend_lock=False,
    )

    summary = report.human_summary()

    assert "fail-1" in summary
    assert "autodev execute timed out after 1800s" in summary
    assert "pilot-report.json" in summary
    # the excerpt is TRUNCATED (last 300 chars) — the marker near the start of
    # the 463-char tail must have fallen off the excerpt entirely.
    assert "MIDDLE_MARKER" not in summary
    assert ("y" * 300) in summary  # the tail end of the excerpt survives intact
    # the FULL tail is never dumped into the .md — only the JSON gets that
    assert long_tail not in summary

    # NEGATIVE CONTROL: split the doc at the failure-detail section header and
    # confirm the clean instance's id does not appear anywhere after it (it
    # legitimately appears earlier, in the per-instance table).
    assert "## Failure detail" in summary
    detail_section = summary.split("## Failure detail", 1)[1]
    assert "clean-1" not in detail_section


def test_pilot_report_excerpt_vs_full_tail_split_between_md_and_json(tmp_path: Path):
    """The excerpt/full split must be genuinely locked: a tail longer than
    _SUMMARY_TAIL_EXCERPT_CHARS is TRUNCATED in the .md but the FULL string is
    still present, verbatim, in pilot-report.json."""
    import json

    from benchmarks.runner.pilot import _SUMMARY_TAIL_EXCERPT_CHARS

    full_tail = "A" * 50 + "B" * (_SUMMARY_TAIL_EXCERPT_CHARS + 50)  # 400 chars
    outcome = PilotInstanceOutcome(
        instance_id="trunc-1",
        status=ERROR,
        wall_time_s=1.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=False,
        quota_exhausted=False,
        detail="boom",
        fail_stdout_tail=full_tail,
        fail_stderr_tail="",
    )
    report = PilotReport(
        run_id="rid-trunc",
        autodev_version="1.0.0",
        timestamp="2026-01-01T00:00:00+00:00",
        instances=[outcome],
        passed=0,
        failed=0,
        errored=1,
        blind_count=0,
        clean_count=0,
        total_wall_time_s=1.0,
        total_quota_wait_time_s=0.0,
        gate_verdict="red",
        gate_status="insufficient",
        gate_reasons=[],
        baseline_established=False,
        baseline_path=None,
        recommend_lock=False,
    )

    json_path, summary_path = write_pilot_report(report, tmp_path / "out")
    md_text = summary_path.read_text(encoding="utf-8")
    doc = json.loads(json_path.read_text(encoding="utf-8"))

    # full tail present, verbatim, in the JSON
    assert doc["instances"][0]["fail_stdout_tail"] == full_tail
    # .md gets only the last _SUMMARY_TAIL_EXCERPT_CHARS chars — the leading
    # "A"*50 run must have fallen off the excerpt entirely.
    assert full_tail not in md_text
    assert ("A" * 50) not in md_text
    assert ("B" * _SUMMARY_TAIL_EXCERPT_CHARS) in md_text


def test_human_summary_failure_block_survives_embedded_backtick_fence():
    """Code-review regression: captured autodev output routinely contains its
    OWN triple-backtick fences (autodev echoes diffs/plans/markdown). A naive
    hardcoded ``` wrapper would be closed early by that embedded fence,
    garbling the rendered section — reproduced and fixed by widening the
    wrapper fence beyond the longest backtick run actually present in the
    excerpt. This locks the block is well-formed: exactly two fence lines,
    identical to each other, strictly longer than any backtick run inside,
    with the full excerpt content intact between them."""
    tricky_tail = (
        "before\n```python\ndef f():\n    return 1\n```\nafter, and a longer "
        "run: ```` still inside ````\nend"
    )
    outcome = PilotInstanceOutcome(
        instance_id="fence-1",
        status=ERROR,
        wall_time_s=1.0,
        quota_wait_time_s=0.0,
        attempts=1,
        blind=False,
        quota_exhausted=False,
        detail="autodev execute exited 1",
        fail_stdout_tail=tricky_tail,
        fail_stderr_tail="",
    )
    report = PilotReport(
        run_id="rid-fence",
        autodev_version="1.0.0",
        timestamp="2026-01-01T00:00:00+00:00",
        instances=[outcome],
        passed=0,
        failed=0,
        errored=1,
        blind_count=0,
        clean_count=0,
        total_wall_time_s=1.0,
        total_quota_wait_time_s=0.0,
        gate_verdict="red",
        gate_status="insufficient",
        gate_reasons=[],
        baseline_established=False,
        baseline_path=None,
        recommend_lock=False,
    )

    summary = report.human_summary()
    section = summary.split("### fence-1", 1)[1]

    # The tail is short enough to render in full (well under the excerpt cap),
    # so every line of it must appear verbatim, unmangled, inside the block.
    for line in tricky_tail.splitlines():
        assert line in section

    # The wrapper is the OUTERMOST pair of bare backtick-only lines — the
    # first and last such line in the section, NOT "exactly 2 total" (the
    # payload legitimately contains its own bare "```" line as part of its
    # embedded fenced block, which a naive count would misclassify).
    section_lines = section.splitlines()
    fence_idx = [
        i for i, line in enumerate(section_lines) if line and set(line) == {"`"}
    ]
    assert len(fence_idx) >= 2, f"expected an opening + closing fence, got {fence_idx!r}"
    opening_idx, closing_idx = fence_idx[0], fence_idx[-1]
    opening, closing = section_lines[opening_idx], section_lines[closing_idx]
    assert opening == closing, "opening and closing wrapper fences must match"

    # The actual guarantee: the wrapper fence is strictly longer than the
    # longest backtick run anywhere in the body it wraps (here, the
    # embedded "````" run of 4) — this is what makes the wrapper unclosable
    # by content, regardless of how many backtick lines the payload itself
    # happens to contain.
    body = "\n".join(section_lines[opening_idx + 1 : closing_idx])
    longest_inner_run = max((len(m) for m in re.findall(r"`+", body)), default=0)
    assert longest_inner_run == 4, "fixture sanity check: expected a run of 4"
    assert len(opening) > longest_inner_run
