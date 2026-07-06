"""Gate tests for the quota-aware pause/resume wrapper (Phase-1 P1.4).

The wrapper's whole reason to exist is a single invariant: **an AutoDev
quota/rate abort must NEVER be turned into a false capability FAIL.** AutoDev
signals a quota/plan-cap abort by tripping the cross-task
:class:`InfraFailureCircuitBreaker` (subtypes ``{auth_failed, rate_limited,
server_error, usage_limit_hit}``; ``usage_limit_hit`` = the monthly/plan cap),
which raises :class:`tournament.errors.InfrastructureCircuitOpenError` and aborts
the run non-zero with an ``infra_circuit_open:`` ledger reason. From the
benchmark's serial solve loop that surfaces either as a raised
``InfrastructureCircuitOpenError`` or as a non-zero-exit
:class:`~benchmarks.runner.solve.SolveOutcome` whose captured output / run ledger
carries the rate-limit signal.

These pin the three required proofs from the plan
(``thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md`` P1.4):

  (a) a simulated ``usage_limit_hit`` on attempt 1 then success on attempt 2 →
      the instance is RESOLVED via retry, and it was NEVER marked FAIL in
      between (ERROR-until-complete during the quota wait);
  (b) the attempt cap is respected — N quota aborts → an ERROR (quota-exhausted),
      never a FAIL, and no infinite loop;
  (c) a genuine capability failure (empty diff / a real FAIL that is NOT a quota
      signal) is NOT retried as if it were quota — the broken control that keeps
      the classifier honest.

Plus: the classifier's own non-vacuous unit proofs, the raised-exception detect
path, propagation of a non-quota exception (no over-broad swallow), the
``max_parallel_subprocesses = 1`` enforcement, and the serial-loop wrapper.

Every test injects a fake clock/sleep + a scripted solve fn. Nothing sleeps for
real, nothing touches the network, no autodev / Claude subprocess runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.runner.quota_guard import (
    COMPLETE,
    DEFAULT_MAX_ATTEMPTS,
    BackoffPolicy,
    GuardResult,
    QuotaWaitEvent,
    enforce_serial_subprocesses,
    is_quota_abort,
    run_guarded_solve,
    run_instance_with_quota_guard,
)
from benchmarks.runner.solve import SolveOutcome, SolveProfile
from benchmarks.scorers.base import ERROR, FAIL, PASS
from tournament.errors import InfrastructureCircuitOpenError

# ---------------------------------------------------------------------------
# Test doubles: a scripted solve fn + a fake sleep (no real time passes).
# ---------------------------------------------------------------------------


class FakeSleep:
    """Records every requested sleep duration; never actually sleeps."""

    def __init__(self) -> None:
        self.durations: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)

    @property
    def calls(self) -> int:
        return len(self.durations)


class ScriptedSolve:
    """A per-attempt scripted solver.

    ``script`` is a list whose i-th entry is what the i-th attempt yields: a
    :class:`SolveOutcome` to return, or a ``BaseException`` to raise. Calling
    more times than the script has entries raises ``AssertionError`` — so an
    infinite retry loop fails loudly instead of hanging.
    """

    def __init__(self, script: list[object]) -> None:
        self.script = script
        self.attempts: list[int] = []

    def __call__(self, attempt_no: int) -> SolveOutcome:
        self.attempts.append(attempt_no)
        idx = len(self.attempts) - 1
        if idx >= len(self.script):
            raise AssertionError(
                f"solve called {len(self.attempts)}x but script has "
                f"{len(self.script)} entries — unbounded retry?"
            )
        item = self.script[idx]
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, SolveOutcome)
        return item

    @property
    def calls(self) -> int:
        return len(self.attempts)


def _outcome(
    *,
    success: bool,
    ledger_path: Path,
    failed_reason: str | None = None,
    stdout: str = "",
    stderr: str = "",
    diff: str = "",
) -> SolveOutcome:
    """Build a :class:`SolveOutcome` for the guard to classify."""
    return SolveOutcome(
        diff=diff,
        base_sha="basesha",
        success=success,
        empty_diff=not (diff and diff.strip()),
        diff_source="commit" if diff else "none",
        ledger_path=ledger_path,
        failed_reason=failed_reason,
        calls=[],
        invocations=1,
        wall_time_s=1.0,
        fail_stdout_tail=stdout,
        fail_stderr_tail=stderr,
    )


def _empty_ledger(tmp_path: Path, name: str = "ledger.jsonl") -> Path:
    """A ledger path that exists but carries no quota signal."""
    p = tmp_path / name
    p.write_text(
        '{"event": "task_completed", "status": "reviewed"}\n', encoding="utf-8"
    )
    return p


# A small, deterministic backoff so delay assertions are exact: 10, 20, 40, 80.
_BACKOFF = BackoffPolicy(initial_s=10.0, multiplier=2.0, max_s=1000.0)


# ---------------------------------------------------------------------------
# enforce_serial_subprocesses — the max_parallel_subprocesses=1 guarantee.
# ---------------------------------------------------------------------------


def test_enforce_serial_forces_parallelism_to_one():
    """A profile that (wrongly) allows burst parallelism is forced to serial,
    and sibling config_patch keys survive the merge."""
    profile = SolveProfile(
        config_patch={
            "tournaments": {"max_parallel_subprocesses": 8, "other": "keep"},
            "qa_gates": {"test_runner": False},
        }
    )
    out = enforce_serial_subprocesses(profile)
    assert out.config_patch["tournaments"]["max_parallel_subprocesses"] == 1
    # sibling keys are preserved (a real deep-merge, not a clobber)
    assert out.config_patch["tournaments"]["other"] == "keep"
    assert out.config_patch["qa_gates"] == {"test_runner": False}


def test_enforce_serial_adds_when_missing():
    """An empty config_patch gains the serial constraint."""
    out = enforce_serial_subprocesses(SolveProfile(config_patch={}))
    assert out.config_patch["tournaments"]["max_parallel_subprocesses"] == 1


def test_enforce_serial_is_idempotent_and_pure():
    """Applying twice equals applying once, and the ORIGINAL profile's mapping
    is never mutated (belt-and-suspenders redundancy must be idempotent)."""
    original = SolveProfile(
        config_patch={"tournaments": {"max_parallel_subprocesses": 8}}
    )
    once = enforce_serial_subprocesses(original)
    twice = enforce_serial_subprocesses(once)
    assert (
        once.config_patch["tournaments"]["max_parallel_subprocesses"]
        == twice.config_patch["tournaments"]["max_parallel_subprocesses"]
        == 1
    )
    # original untouched — the caller's profile is not aliased/mutated
    assert original.config_patch["tournaments"]["max_parallel_subprocesses"] == 8


# ---------------------------------------------------------------------------
# is_quota_abort — the load-bearing classifier (non-vacuous both ways).
# ---------------------------------------------------------------------------


def test_is_quota_abort_success_is_never_quota(tmp_path: Path):
    """A successful solve is NEVER a quota abort — even if the (stale) ledger
    happens to carry the token. ``success`` short-circuits the classifier."""
    ledger = tmp_path / "l.jsonl"
    ledger.write_text('{"blocked_reason": "infra_circuit_open: x"}\n', "utf-8")
    out = _outcome(success=True, ledger_path=ledger, diff="patch")
    assert is_quota_abort(out) is False


def test_is_quota_abort_detects_ledger_signal(tmp_path: Path):
    """Non-zero exit + the ledger's ``infra_circuit_open`` reason → quota."""
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(
        '{"status": "quarantined", "blocked_reason": "infra_circuit_open: '
        'circuit open"}\n',
        "utf-8",
    )
    out = _outcome(
        success=False, ledger_path=ledger, failed_reason="autodev execute exited 1"
    )
    assert is_quota_abort(out) is True


def test_is_quota_abort_detects_usage_limit_in_captured_output(tmp_path: Path):
    """Non-zero exit + the ``usage_limit_hit`` (monthly cap) token in the
    captured stderr → quota, even when the ledger is silent."""
    out = _outcome(
        success=False,
        ledger_path=_empty_ledger(tmp_path),
        failed_reason="autodev execute exited 1",
        stderr="adapter halt: usage_limit_hit — plan cap reached",
    )
    assert is_quota_abort(out) is True


def test_is_quota_abort_capability_failure_is_not_quota(tmp_path: Path):
    """THE broken control for the classifier: a real capability failure —
    non-zero exit, empty diff, a plain test-assertion reason, NO rate-limit
    signal anywhere — must NOT be classified as a quota abort."""
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(
        '{"event": "test_failed", "blocked_reason": "tests red: assert 1 == 2"}\n',
        "utf-8",
    )
    out = _outcome(
        success=False,
        ledger_path=ledger,
        failed_reason="autodev execute exited 1",
        stderr="AssertionError: expected 2 got 1",
    )
    assert is_quota_abort(out) is False


# ---------------------------------------------------------------------------
# (a) quota abort → sleep → retry → RESOLVED, never a FAIL in between.
# ---------------------------------------------------------------------------


def test_gate_a_usage_limit_then_success_resolves_via_retry(tmp_path: Path):
    quota = _outcome(
        success=False,
        ledger_path=_empty_ledger(tmp_path, "l1.jsonl"),
        failed_reason="autodev execute exited 1",
        stderr="usage_limit_hit: monthly plan cap reached",
    )
    win = _outcome(
        success=True, ledger_path=_empty_ledger(tmp_path, "l2.jsonl"), diff="the fix"
    )
    solver = ScriptedSolve([quota, win])
    sleep = FakeSleep()
    events: list[QuotaWaitEvent] = []

    result = run_instance_with_quota_guard(
        "inst-1",
        solver,
        max_attempts=5,
        backoff=_BACKOFF,
        sleep=sleep,
        on_quota_wait=events.append,
    )

    # RESOLVED via retry — landed on the attempt-2 SUCCESS, not the abort.
    assert result.status == COMPLETE
    assert result.outcome is win
    assert solver.calls == 2
    assert result.attempts == 2
    # It slept exactly once (between the abort and the retry), toward the window.
    assert sleep.calls == 1
    assert sleep.durations == [10.0]
    assert result.quota_waits == 1
    assert result.quota_wait_time_s == 10.0
    # NEVER a FAIL in between: the only in-flight signal was ERROR-until-complete.
    assert result.status not in (FAIL, PASS)
    assert len(events) == 1
    assert events[0].status == ERROR
    assert all(e.status != FAIL for e in events)


def test_gate_a_raised_circuit_open_then_success(tmp_path: Path):
    """The *raised-exception* detect path: an in-process
    ``InfrastructureCircuitOpenError`` on attempt 1 is a quota abort too —
    retried, resolved, never FAILed."""
    win = _outcome(success=True, ledger_path=_empty_ledger(tmp_path), diff="fix")
    solver = ScriptedSolve(
        [InfrastructureCircuitOpenError("infrastructure circuit open — 3 in 60s"), win]
    )
    sleep = FakeSleep()

    result = run_instance_with_quota_guard(
        "inst-2", solver, max_attempts=4, backoff=_BACKOFF, sleep=sleep
    )

    assert result.status == COMPLETE
    assert result.outcome is win
    assert result.attempts == 2
    assert sleep.calls == 1


# ---------------------------------------------------------------------------
# (b) attempt cap respected → ERROR (quota-exhausted), not FAIL, bounded.
# ---------------------------------------------------------------------------


def test_gate_b_attempt_cap_exhausts_to_error_not_fail(tmp_path: Path):
    quota = _outcome(
        success=False,
        ledger_path=_empty_ledger(tmp_path),
        failed_reason="autodev execute exited 1",
        stderr="rate_limited: slow down",
    )
    cap = 3
    solver = ScriptedSolve([quota, quota, quota])  # exactly cap entries
    sleep = FakeSleep()

    result = run_instance_with_quota_guard(
        "inst-3", solver, max_attempts=cap, backoff=_BACKOFF, sleep=sleep
    )

    # Cap-exceeded on pure quota aborts → ERROR quota-exhausted, NEVER FAIL.
    assert result.status == ERROR
    assert result.quota_exhausted is True
    assert result.status != FAIL
    assert "quota-exhausted" in (result.detail or "")
    # Bounded: exactly ``cap`` attempts, sleeping only BETWEEN them (cap-1),
    # never after the final attempt — no infinite loop.
    assert solver.calls == cap
    assert result.attempts == cap
    assert sleep.calls == cap - 1
    assert sleep.durations == [10.0, 20.0]  # backoff toward the next window
    assert result.quota_waits == cap - 1
    assert result.quota_wait_time_s == 30.0


# ---------------------------------------------------------------------------
# (c) a genuine capability failure is NOT retried as if it were quota.
# ---------------------------------------------------------------------------


def test_gate_c_capability_failure_is_not_retried(tmp_path: Path):
    """A real FAIL / empty-diff (no quota signal) is terminal on the first
    attempt: the guard must NOT sleep or re-run it as if it were a quota abort.
    If it did, a genuine regression would be silently masked as a quota wait."""
    dud = _outcome(
        success=False,
        ledger_path=_empty_ledger(tmp_path),
        failed_reason="autodev execute exited 1",
        stderr="AssertionError: 1 != 2",  # capability signal, NOT quota
    )
    solver = ScriptedSolve([dud])  # only ONE entry: a 2nd call would AssertionError
    sleep = FakeSleep()
    events: list[QuotaWaitEvent] = []

    result = run_instance_with_quota_guard(
        "inst-4",
        solver,
        max_attempts=5,
        backoff=_BACKOFF,
        sleep=sleep,
        on_quota_wait=events.append,
    )

    assert result.status == COMPLETE  # terminal — handed downstream, not retried
    assert result.outcome is dud
    assert solver.calls == 1  # exactly one attempt — NOT retried
    assert sleep.calls == 0  # never slept
    assert result.quota_waits == 0
    assert result.quota_exhausted is False
    assert events == []  # no ERROR-until-complete waits fired


def test_non_quota_exception_propagates(tmp_path: Path):
    """A non-quota exception raised by the solver must NOT be swallowed as a
    quota abort — the guard's except is not over-broad."""
    solver = ScriptedSolve([ValueError("bug in adapter")])
    sleep = FakeSleep()
    with pytest.raises(ValueError, match="bug in adapter"):
        run_instance_with_quota_guard(
            "inst-5", solver, max_attempts=3, backoff=_BACKOFF, sleep=sleep
        )
    assert sleep.calls == 0  # never treated as quota


def test_default_max_attempts_is_sane():
    """The configurable cap has a sane, >1 default (so the wrapper actually
    retries by default) — a regression to 1 would disable quota resilience."""
    assert DEFAULT_MAX_ATTEMPTS >= 2


# ---------------------------------------------------------------------------
# The serial-loop wrapper: enforces max_parallel=1 AND applies the guard.
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """A minimal BenchmarkAdapter double: records the profile handed to solve,
    and predicts a trivial record from the outcome's diff."""

    name = "fake"
    model_name = "autodev"

    def __init__(self) -> None:
        self.profiles_seen: list[SolveProfile] = []

    def prepare(self, instance, workdir: Path) -> SolveProfile:
        # Deliberately request burst parallelism so the wrapper must override it.
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


def test_run_guarded_solve_enforces_serial_and_retries(tmp_path: Path):
    adapter = _FakeAdapter()
    sleep = FakeSleep()
    ledger = _empty_ledger(tmp_path)

    quota = _outcome(
        success=False,
        ledger_path=ledger,
        failed_reason="autodev execute exited 1",
        stderr="usage_limit_hit",
    )
    win = _outcome(success=True, ledger_path=ledger, diff="+the fix\n")

    # scripted solve_fn: instance-0 aborts once then resolves.
    seq = [quota, win]
    seen_profiles: list[SolveProfile] = []

    def fake_solve_fn(workdir, intent, profile, invoker):
        seen_profiles.append(profile)
        return seq[len(seen_profiles) - 1]

    instances = [{"instance_id": "abc-1", "problem_statement": "fix it"}]
    predictions, results = run_guarded_solve(
        adapter,
        instances,
        invoker=lambda *a, **k: None,  # never called by fake_solve_fn
        workdir_root=tmp_path / "wd",
        solve_fn=fake_solve_fn,
        backoff=_BACKOFF,
        sleep=sleep,
    )

    # every profile the wrapper handed to solve was forced to serial
    assert seen_profiles, "solve_fn was never called"
    assert all(
        p.config_patch["tournaments"]["max_parallel_subprocesses"] == 1
        for p in seen_profiles
    )
    # the quota abort was retried to success; one prediction emitted with the fix
    assert len(results) == 1
    assert results[0].status == COMPLETE
    assert len(predictions) == 1
    assert predictions[0]["instance_id"] == "abc-1"
    assert predictions[0]["model_patch"] == "+the fix\n"
    assert sleep.calls == 1


def test_run_guarded_solve_quota_exhausted_emits_error_not_fail(tmp_path: Path):
    """When an instance never clears the quota within the cap, the wrapper emits
    an ERROR result (not FAIL) and still writes a (patch-less) prediction so the
    instance is accounted for, never silently dropped."""
    adapter = _FakeAdapter()
    sleep = FakeSleep()
    ledger = _empty_ledger(tmp_path)
    quota = _outcome(
        success=False,
        ledger_path=ledger,
        failed_reason="autodev execute exited 1",
        stderr="usage_limit_hit",
    )

    def always_quota(workdir, intent, profile, invoker):
        return quota

    instances = [{"instance_id": "abc-2", "problem_statement": "fix it"}]
    predictions, results = run_guarded_solve(
        adapter,
        instances,
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        solve_fn=always_quota,
        max_attempts=2,
        backoff=_BACKOFF,
        sleep=sleep,
    )

    assert results[0].status == ERROR
    assert results[0].quota_exhausted is True
    assert results[0].status != FAIL
    # instance is still accounted for in predictions (empty patch → scorer ERROR)
    assert len(predictions) == 1
    assert predictions[0]["instance_id"] == "abc-2"
    assert sleep.calls == 1  # cap=2 → one wait between the two attempts


def test_predict_receives_recorded_winning_workdir_not_reconstructed(tmp_path: Path):
    """The workdir handed to ``adapter.predict`` must be the EXACT path the guard
    solved the winning attempt in — recorded and threaded through
    ``GuardResult.workdir`` — not a value rebuilt by string-formatting
    ``inst_{index}_try_{attempts}`` (a silent desync footgun if either format
    drifts, which would feed the WRONG patch to scoring).

    Non-vacuous: the instance quota-aborts once then resolves on attempt 2, so the
    winning workdir is the SECOND solve's (``try_02``). Before the fix
    ``GuardResult`` had no ``workdir`` field at all, so ``results[0].workdir`` here
    raised ``AttributeError`` — the fix records the real solved path."""
    solved: list[Path] = []
    predicted: list[Path] = []

    class _WorkdirProbeAdapter:
        name = "wd-probe"
        model_name = "autodev"

        def __init__(self) -> None:
            self.reports: list = []

        def prepare(self, instance, workdir: Path) -> SolveProfile:
            return SolveProfile(config_patch={})

        def intent(self, instance) -> str:
            return str(instance["problem_statement"])

        def predict(self, instance, workdir: Path, outcome: SolveOutcome):
            predicted.append(Path(workdir))
            return {
                "instance_id": str(instance["instance_id"]),
                "model_name_or_path": self.model_name,
                "model_patch": outcome.diff,
            }

    ledger = _empty_ledger(tmp_path)
    quota = _outcome(
        success=False,
        ledger_path=ledger,
        failed_reason="autodev execute exited 1",
        stderr="usage_limit_hit",
    )
    win = _outcome(success=True, ledger_path=ledger, diff="the winning fix")
    seq = [quota, win]

    def solve_fn(workdir, intent, profile, invoker):
        solved.append(Path(workdir))
        return seq[len(solved) - 1]

    instances = [{"instance_id": "wd-1", "problem_statement": "fix it"}]
    predictions, results = run_guarded_solve(
        _WorkdirProbeAdapter(),
        instances,
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "root",
        solve_fn=solve_fn,
        backoff=_BACKOFF,
        sleep=FakeSleep(),
    )

    # The guard recorded the ACTUAL winning workdir (the attempt-2 solve).
    assert results[0].workdir is not None
    assert results[0].workdir == solved[-1]
    # predict got the EXACT path the winning attempt solved in — not try_01, not a
    # reconstruction. If the two ever desynced, predict would see a different path.
    assert predicted == [solved[-1]]
    assert predictions[0]["model_patch"] == "the winning fix"


def test_run_guarded_solve_isolates_bad_instance_prepare_and_continues(tmp_path: Path):
    """A per-instance ``InstancePrepareError`` (the contract signal for an expected
    setup failure, e.g. an invalid ``base_commit``) must be recorded ERROR for THAT
    instance while the sweep CONTINUES — one bad instance can never abort the whole
    overnight run (the pilot's #1 job is screening *which* instances even run).

    Non-vacuous: before the isolation the exception propagated out of
    ``run_guarded_solve`` (through the guard's re-raise of non-quota exceptions), so
    this call raised instead of returning results for all three instances."""
    from benchmarks.adapters.base import InstancePrepareError

    class _MixedAdapter:
        name = "mixed"
        model_name = "autodev"

        def __init__(self) -> None:
            self.reports: list = []

        def prepare(self, instance, workdir: Path) -> SolveProfile:
            if str(instance["instance_id"]) == "bad-2":
                raise InstancePrepareError("invalid base_commit deadbeef")
            return SolveProfile(config_patch={})

        def intent(self, instance) -> str:
            return "x"

        def predict(self, instance, workdir: Path, outcome: SolveOutcome):
            return {
                "instance_id": str(instance["instance_id"]),
                "model_name_or_path": self.model_name,
                "model_patch": outcome.diff,
            }

    win = _outcome(success=True, ledger_path=_empty_ledger(tmp_path), diff="fix")

    def solve_fn(workdir, intent, profile, invoker):
        return win

    instances = [
        {"instance_id": "ok-1", "problem_statement": "p"},
        {"instance_id": "bad-2", "problem_statement": "p"},
        {"instance_id": "ok-3", "problem_statement": "p"},
    ]
    predictions, results = run_guarded_solve(
        _MixedAdapter(),
        instances,
        invoker=lambda *a, **k: None,
        workdir_root=tmp_path / "wd",
        solve_fn=solve_fn,
        backoff=_BACKOFF,
        sleep=FakeSleep(),
    )

    # the sweep COMPLETED over all three (not aborted by the bad instance)
    assert len(results) == 3
    assert len(predictions) == 3
    by_id = {r.instance_id: r for r in results}
    # instances #1 and #3 solved normally...
    assert by_id["ok-1"].status == COMPLETE
    assert by_id["ok-3"].status == COMPLETE
    # ...instance #2 is ERROR (accounted for, no outcome/workdir).
    assert by_id["bad-2"].status == ERROR
    assert by_id["bad-2"].status != FAIL
    assert by_id["bad-2"].outcome is None
    # patch-less prediction present for the bad instance — never a silent drop.
    preds_by_id = {p["instance_id"]: p for p in predictions}
    assert preds_by_id["bad-2"]["model_patch"] == ""
    assert preds_by_id["ok-1"]["model_patch"] == "fix"
    assert preds_by_id["ok-3"]["model_patch"] == "fix"


def test_run_guarded_solve_unexpected_prepare_exception_still_propagates(
    tmp_path: Path,
):
    """Over-catch guard: an adapter ``prepare`` that raises a NON-contract exception
    (a genuine harness bug — a plain ``RuntimeError``, which is NOT an
    ``InstancePrepareError``) must still abort loudly, never be swallowed as a
    per-instance ERROR. If the isolation used a bare ``except Exception`` this would
    fail (the RuntimeError would be silently masked)."""

    class _BuggyAdapter:
        name = "buggy"
        model_name = "autodev"

        def __init__(self) -> None:
            self.reports: list = []

        def prepare(self, instance, workdir: Path) -> SolveProfile:
            raise RuntimeError("genuine harness bug")

        def intent(self, instance) -> str:
            return "x"

        def predict(self, instance, workdir: Path, outcome: SolveOutcome):
            return {}

    instances = [{"instance_id": "x-1", "problem_statement": "p"}]
    with pytest.raises(RuntimeError, match="genuine harness bug"):
        run_guarded_solve(
            _BuggyAdapter(),
            instances,
            invoker=lambda *a, **k: None,
            workdir_root=tmp_path / "wd",
            solve_fn=lambda *a, **k: None,
            backoff=_BACKOFF,
            sleep=FakeSleep(),
        )


def test_guard_result_is_dataclass_shape():
    """The result carries the telemetry the coarse gate (P1.5) reads:
    per-instance status + attempts + quota-wait accounting."""
    r = GuardResult(
        instance_id="x",
        status=COMPLETE,
        outcome=None,
        attempts=1,
        quota_waits=0,
        quota_wait_time_s=0.0,
    )
    assert r.instance_id == "x"
    assert r.quota_exhausted is False
