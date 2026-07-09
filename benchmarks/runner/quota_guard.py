"""Quota-aware pause/resume wrapper for the serial solve loop (Phase-1 P1.4).

**The one invariant this module exists to guarantee: an AutoDev quota/rate abort
is NEVER turned into a false capability FAIL.**

AutoDev bursts many parallel ``claude -p`` calls per task and aborts when it hits
the subscription cap. That abort is signalled by the cross-task
:class:`orchestrator.circuit_breaker.InfraFailureCircuitBreaker` tripping on an
infrastructure-class subtype (``auth_failed`` / ``rate_limited`` /
``server_error`` / ``usage_limit_hit`` — the last being the *monthly/plan* cap),
which raises :class:`tournament.errors.InfrastructureCircuitOpenError`, quarantines
the in-flight task, parks the phase at ``review_status="paused"`` and exits the run
non-zero with a ``blocked_reason`` prefixed ``infra_circuit_open:``.

From the benchmark's serial per-instance solve loop that surfaces two ways:

  1. as a **raised** ``InfrastructureCircuitOpenError`` (an in-process driver), or
  2. as a **non-zero-exit** :class:`~benchmarks.runner.solve.SolveOutcome` whose
     captured output / run ledger carries the rate-limit signal (the subprocess
     path — ``solve`` captures the non-zero exit into
     ``success=False`` + ``failed_reason`` / ``fail_stderr_tail`` and records the
     ``ledger_path``).

On a detected quota abort the wrapper **sleeps** (backoff toward the next quota
window — the sleep fn is injectable so tests never actually sleep) and **re-runs
that same instance**. Throughout the wait the instance is *ERROR-until-complete*
— it is never emitted as a FAIL. Attempts are capped per instance
(:data:`DEFAULT_MAX_ATTEMPTS`, configurable); on cap-exceeded the instance is
recorded as an **ERROR (quota-exhausted)**, still never a FAIL.

The wrapper also forces ``tournaments.max_parallel_subprocesses = 1`` onto every
profile it solves (:func:`enforce_serial_subprocesses`) — a belt-and-suspenders,
idempotent guarantee that the within-task burst against the subscription cap is
cut even if the adapter forgot to set it.

Nothing here judges PASS vs FAIL — that is the scorer's job on a produced
candidate patch. The wrapper's terminal states are exactly :data:`COMPLETE`
(reached a non-quota terminal outcome; hand it downstream to ``predict``/score)
and :data:`ERROR` (quota-exhausted) — never PASS, never FAIL.
"""

from __future__ import annotations

import copy
import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from benchmarks.adapters.base import (
    BenchmarkAdapter,
    Instance,
    InstancePrepareError,
)
from benchmarks.runner.solve import (
    SolveInvoker,
    SolveOutcome,
    SolveProfile,
    solve,
)
from benchmarks.scorers.base import ERROR

# Guard terminal status for "reached a non-quota terminal outcome". Distinct from
# the scorer's PASS/FAIL/ERROR: a COMPLETE outcome is handed downstream, where
# ``predict`` maps an empty source residual to ERROR and a candidate patch to the
# scorer's PASS/FAIL. The guard itself never emits PASS or FAIL; ERROR (reused
# from the scorer vocabulary) is emitted only for the quota-exhausted terminal.
COMPLETE = "COMPLETE"

# Default per-instance attempt cap: how many times the wrapper will re-run one
# instance across quota windows before recording ERROR (quota-exhausted). >1 so
# the wrapper actually retries by default; the pilot (P1.6) tunes it against the
# measured subscription throughput. A caller may source this from
# ``SolveProfile.max_attempts`` (the contract that lets the cap travel with the
# profile) by passing ``max_attempts=profile.max_attempts``.
DEFAULT_MAX_ATTEMPTS = 6

# Machine tokens that mark a quota / rate / plan-cap abort in a failing solve's
# captured output or run ledger. Deliberately specific log/ledger tokens — NOT
# generic words like "limit" — so a plain capability failure (a red test, an
# ``AssertionError``) is never misread as quota. ``infra_circuit_open`` is the
# ledger ``blocked_reason`` prefix stamped for ANY breaker trip; the two subtype
# tokens catch the signal when it surfaces in adapter/console output without the
# prefix; the spaced form matches the breaker's ``should_halt`` message. Kept
# lowercase for case-insensitive matching and injectable so the pilot can widen
# it (e.g. add ``server_error``) without editing this module.
QUOTA_SIGNAL_TOKENS: frozenset[str] = frozenset(
    {
        "infra_circuit_open",
        "infrastructure circuit open",
        "infrastructurecircuitopenerror",
        "usage_limit_hit",
        "rate_limited",
    }
)


# One solve attempt for an instance: given the 1-based attempt number, run a full
# solve and return its outcome, or raise ``InfrastructureCircuitOpenError`` (the
# raised-signal quota path). The attempt number lets the closure use a fresh
# per-attempt workdir when it re-prepares the instance.
SolveAttempt = Callable[[int], SolveOutcome]

# Injectable sleep (default: real ``time.sleep``). Tests inject a fake so no wall
# time passes; the requested durations double as the quota-wait accounting.
SleepFn = Callable[[float], None]

# Injectable solve fn used by :func:`run_guarded_solve` (default: the real
# :func:`benchmarks.runner.solve.solve`), so the loop wrapper is unit-testable
# with a scripted in-memory solver and no git/autodev.
SolveFn = Callable[[Path, str, SolveProfile, SolveInvoker], SolveOutcome]


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential backoff toward the next quota window.

    ``delay_for(n)`` is the sleep before the ``n``-th quota wait (1-based):
    ``min(initial_s * multiplier ** (n - 1), max_s)``. Defaults sleep 5 min then
    double, capped at 1 h — a subscription's plan cap resets on a window boundary,
    so a coarse climbing wait is the honest Phase-1 shape; the pilot tunes it.
    """

    initial_s: float = 300.0
    multiplier: float = 2.0
    max_s: float = 3600.0

    def delay_for(self, wait_index: int) -> float:
        idx = max(1, wait_index)
        delay = self.initial_s * (self.multiplier ** (idx - 1))
        return float(min(delay, self.max_s))


@dataclass(frozen=True)
class QuotaWaitEvent:
    """One ERROR-until-complete quota wait (telemetry hook payload).

    Emitted just before the wrapper sleeps and re-runs an instance. ``status`` is
    always :data:`ERROR` — the instance is in-flight, never a FAIL — so a consumer
    (live telemetry / the coarse-gate report) can surface "waiting on quota"
    honestly without any path ever reading it as a capability verdict.
    """

    instance_id: str
    attempt: int
    sleep_s: float
    status: str = ERROR
    detail: str | None = None


@dataclass
class GuardResult:
    """Terminal per-instance outcome of the quota-aware wrapper.

    ``status`` is :data:`COMPLETE` (a non-quota terminal outcome was reached —
    hand ``outcome`` to ``predict``/score) or :data:`ERROR` (quota-exhausted).
    **Never PASS, never FAIL** — the wrapper does not judge capability. The
    remaining fields are the telemetry the coarse gate (P1.5) reads:
    ``attempts`` (solve calls made), ``quota_waits`` + ``quota_wait_time_s`` (how
    much of the run was spent parked on quota), and ``quota_exhausted`` (True iff
    the cap was hit on pure quota aborts).

    ``workdir`` is the ACTUAL workdir the winning attempt solved in (recorded by
    the guard, not reconstructed by string-formatting) — the caller feeds exactly
    this path to ``adapter.predict`` so the scored patch can never desync from the
    solved tree. ``None`` when no attempt ran a solve (e.g. a raised quota abort).

    ``fail_stdout_tail`` / ``fail_stderr_tail`` mirror the terminal
    :class:`SolveOutcome`'s captured-failure tails (see ``solve.py``'s
    ``_FAIL_OUTPUT_TAIL``) so a report built from this result is self-diagnosing
    without re-reading the instance's workdir by hand. Both default to ``""``:
    populated from the terminal outcome when one exists (the COMPLETE site always
    has one; the quota-exhausted site has one only if some attempt actually
    returned rather than raising); left at ``""`` when isolation catches an
    ``InstancePrepareError`` before any autodev subprocess ever ran (there is
    genuinely nothing captured to surface).
    """

    instance_id: str
    status: str
    outcome: SolveOutcome | None
    attempts: int
    quota_waits: int
    quota_wait_time_s: float
    detail: str | None = None
    quota_exhausted: bool = False
    workdir: Path | None = None
    fail_stdout_tail: str = ""
    fail_stderr_tail: str = ""


# ---------------------------------------------------------------------------
# max_parallel_subprocesses = 1 enforcement (belt-and-suspenders, idempotent)
# ---------------------------------------------------------------------------


def enforce_serial_subprocesses(profile: SolveProfile) -> SolveProfile:
    """Return a copy of ``profile`` whose ``config_patch`` forces
    ``tournaments.max_parallel_subprocesses = 1``.

    Idempotent and pure: sibling ``config_patch`` keys are preserved (deep-merge,
    not clobber), the caller's profile mapping is never mutated (a deep copy is
    taken), and applying it twice equals applying it once. This is the redundant
    guarantee — even if the adapter already set the value (it does), the wrapper
    re-asserts it so the within-task burst against the subscription cap is cut
    regardless of how the profile was built.
    """
    patched: dict = copy.deepcopy(dict(profile.config_patch))
    tournaments = patched.get("tournaments")
    if not isinstance(tournaments, dict):
        tournaments = {}
        patched["tournaments"] = tournaments
    tournaments["max_parallel_subprocesses"] = 1
    return dataclasses.replace(profile, config_patch=patched)


# ---------------------------------------------------------------------------
# Quota-abort classification (the load-bearing correctness surface)
# ---------------------------------------------------------------------------


def _text_has_signal(text: str, signal_tokens: frozenset[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(tok in lowered for tok in signal_tokens)


def _ledger_has_signal(ledger_path: Path, signal_tokens: frozenset[str]) -> bool:
    """Best-effort scan of the run ledger for a quota signal.

    Never raises — a missing / unreadable ledger simply yields ``False`` (no
    signal found), so a filesystem hiccup can never manufacture a phantom quota
    abort out of a real capability failure.
    """
    try:
        if not ledger_path.is_file():
            return False
        return _text_has_signal(
            ledger_path.read_text(encoding="utf-8", errors="replace"), signal_tokens
        )
    except OSError:
        return False


def is_quota_abort(
    outcome: SolveOutcome,
    *,
    signal_tokens: frozenset[str] = QUOTA_SIGNAL_TOKENS,
) -> bool:
    """Classify a :class:`SolveOutcome` as a quota/rate abort (vs a real
    capability outcome).

    A **successful** solve (every autodev command exited 0) is NEVER a quota abort
    — ``success`` short-circuits, so a stale token in the ledger can't reclassify
    a clean run. Only a non-zero-exit / timed-out solve is a candidate, and then
    only when a rate-limit / circuit signal is present in the captured failure
    output OR the run ledger. A non-zero exit with NO signal (a red test, a crash,
    a timeout) is a capability/other terminal — NOT quota — and must not be
    retried as if the quota window would fix it.
    """
    if outcome.success:
        return False
    captured = " ".join(
        part
        for part in (
            outcome.failed_reason or "",
            outcome.fail_stdout_tail or "",
            outcome.fail_stderr_tail or "",
        )
        if part
    )
    if _text_has_signal(captured, signal_tokens):
        return True
    return _ledger_has_signal(outcome.ledger_path, signal_tokens)


def _quota_exception_types() -> tuple[type[BaseException], ...]:
    """Lazily resolve the in-process quota-abort exception type.

    Imported inside the function (never at module top level) so importing this
    module stays hermetic and light even where ``src`` is not on the path.
    Returns an empty tuple if the import fails; the name-based fallback in
    :func:`_is_quota_exception` still catches it.
    """
    try:
        from tournament.errors import (  # noqa: PLC0415
            InfrastructureCircuitOpenError,
        )

        return (InfrastructureCircuitOpenError,)
    except Exception:  # noqa: BLE001 - src not importable in some contexts
        return ()


def _is_quota_exception(exc: BaseException) -> bool:
    """True iff ``exc`` is AutoDev's in-process quota/circuit abort.

    Matches by type when the class is importable, else by class name — so a
    genuinely unrelated exception (a bug in the adapter, a ``ValueError``) is
    NEVER swallowed as a quota abort and instead propagates to the caller.
    """
    types = _quota_exception_types()
    if types and isinstance(exc, types):
        return True
    return type(exc).__name__ == "InfrastructureCircuitOpenError"


# ---------------------------------------------------------------------------
# The per-instance pause/resume core
# ---------------------------------------------------------------------------


def run_instance_with_quota_guard(
    instance_id: str,
    solve_attempt: SolveAttempt,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: BackoffPolicy = BackoffPolicy(),
    sleep: SleepFn = time.sleep,
    signal_tokens: frozenset[str] = QUOTA_SIGNAL_TOKENS,
    on_quota_wait: Callable[[QuotaWaitEvent], None] | None = None,
) -> GuardResult:
    """Solve one instance, pausing+resuming around quota aborts, up to a cap.

    ``solve_attempt(attempt_no)`` runs one full solve and returns its
    :class:`SolveOutcome`, or raises ``InfrastructureCircuitOpenError``. On a
    detected quota abort (raised, or a non-quota-signalled outcome — see
    :func:`is_quota_abort`) the wrapper emits an ERROR-until-complete
    :class:`QuotaWaitEvent`, sleeps ``backoff.delay_for(...)`` (toward the next
    quota window), and re-runs — never marking the instance FAIL. A non-quota
    outcome is terminal (:data:`COMPLETE`) on the first attempt; the cap bounds
    pure-quota retries and yields ERROR (quota-exhausted) with never a FAIL.

    A non-quota exception from ``solve_attempt`` propagates unchanged (it is not a
    quota abort and must not be silently retried away).
    """
    cap = max(1, max_attempts)
    attempts = 0
    quota_waits = 0
    quota_wait_time_s = 0.0
    last_outcome: SolveOutcome | None = None

    for attempt_no in range(1, cap + 1):
        attempts += 1
        try:
            outcome = solve_attempt(attempt_no)
        except Exception as exc:  # noqa: BLE001 - re-raised unless a quota abort
            if not _is_quota_exception(exc):
                raise
            # raised-signal quota abort: no outcome to hand downstream
        else:
            last_outcome = outcome
            if not is_quota_abort(outcome, signal_tokens=signal_tokens):
                # Reached a non-quota terminal outcome — hand it downstream, along
                # with the EXACT workdir this (winning) attempt solved in.
                return GuardResult(
                    instance_id=instance_id,
                    status=COMPLETE,
                    outcome=outcome,
                    attempts=attempts,
                    quota_waits=quota_waits,
                    quota_wait_time_s=quota_wait_time_s,
                    detail=outcome.failed_reason,
                    workdir=_winning_workdir(solve_attempt),
                    fail_stdout_tail=outcome.fail_stdout_tail,
                    fail_stderr_tail=outcome.fail_stderr_tail,
                )

        # --- here: this attempt was a quota abort (raised or classified) ---
        if attempt_no < cap:
            delay = backoff.delay_for(quota_waits + 1)
            if on_quota_wait is not None:
                on_quota_wait(
                    QuotaWaitEvent(
                        instance_id=instance_id,
                        attempt=attempt_no,
                        sleep_s=delay,
                        status=ERROR,
                        detail="quota abort — sleeping toward next window",
                    )
                )
            sleep(delay)
            quota_waits += 1
            quota_wait_time_s += delay
            continue
        # final attempt was a quota abort → fall through to quota-exhausted

    return GuardResult(
        instance_id=instance_id,
        status=ERROR,
        outcome=last_outcome,
        attempts=attempts,
        quota_waits=quota_waits,
        quota_wait_time_s=quota_wait_time_s,
        detail=(
            f"quota-exhausted after {attempts} attempts "
            f"({quota_waits} quota waits, {quota_wait_time_s:.0f}s parked)"
        ),
        quota_exhausted=True,
        # last_outcome is None when EVERY attempt raised an in-process quota
        # exception (never returned a SolveOutcome to capture tails from) —
        # fall back to "" rather than a None attribute access.
        fail_stdout_tail=last_outcome.fail_stdout_tail if last_outcome else "",
        fail_stderr_tail=last_outcome.fail_stderr_tail if last_outcome else "",
    )


# ---------------------------------------------------------------------------
# The serial-loop wrapper
# ---------------------------------------------------------------------------


class _InstanceAttempt:
    """The per-attempt solve closure for one instance (a :data:`SolveAttempt`).

    Each attempt re-prepares the instance into a fresh per-attempt workdir (a
    quota-aborted autodev run leaves partial state, so a clean re-prepare is the
    honest re-run) and forces the profile to serial before solving.

    It ALSO records ``last_workdir`` — the exact path the most-recent attempt
    solved in — so the guard can return the ACTUAL winning workdir on
    :data:`GuardResult` rather than rebuilding it from a format string that must
    stay in lockstep with this one (a silent desync footgun that would feed the
    wrong patch to ``predict``/scoring).
    """

    def __init__(
        self,
        adapter: BenchmarkAdapter,
        instance: Instance,
        invoker: SolveInvoker,
        workdir_root: Path,
        index: int,
        solve_fn: SolveFn,
    ) -> None:
        self._adapter = adapter
        self._instance = instance
        self._invoker = invoker
        self._workdir_root = workdir_root
        self._index = index
        self._solve_fn = solve_fn
        self.last_workdir: Path | None = None

    def __call__(self, attempt_no: int) -> SolveOutcome:
        workdir = self._workdir_root / f"inst_{self._index:04d}_try_{attempt_no:02d}"
        self.last_workdir = workdir
        profile = enforce_serial_subprocesses(
            self._adapter.prepare(self._instance, workdir)
        )
        intent = self._adapter.intent(self._instance)
        return self._solve_fn(workdir, intent, profile, self._invoker)


def _winning_workdir(solve_attempt: SolveAttempt) -> Path | None:
    """The workdir the (winning) attempt solved in, if the attempt recorded one.

    Reads :attr:`_InstanceAttempt.last_workdir` defensively so a bare scripted
    ``SolveAttempt`` (the unit tests) that never builds a workdir simply yields
    ``None`` — never a reconstruction.
    """
    wd = getattr(solve_attempt, "last_workdir", None)
    return wd if isinstance(wd, Path) else None


def run_guarded_solve(
    adapter: BenchmarkAdapter,
    instances: Iterable[Instance],
    invoker: SolveInvoker,
    *,
    workdir_root: Path,
    solve_fn: SolveFn = solve,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: BackoffPolicy = BackoffPolicy(),
    sleep: SleepFn = time.sleep,
    signal_tokens: frozenset[str] = QUOTA_SIGNAL_TOKENS,
    on_quota_wait: Callable[[QuotaWaitEvent], None] | None = None,
) -> tuple[list[dict], list[GuardResult]]:
    """Quota-aware version of the external solve loop (mirrors
    :func:`benchmarks.runner.external.run_solve`, one instance at a time).

    Solves each instance serially under the quota guard: forces
    ``max_parallel_subprocesses = 1`` on every profile, retries a quota abort with
    backoff, and records ERROR (quota-exhausted) rather than FAIL when the cap is
    hit. Returns ``(predictions, guard_results)``. Every instance is accounted for
    in ``predictions`` — a quota-exhausted instance still emits a patch-less record
    (empty ``model_patch`` → the scorer marks it ERROR, never a silent drop) so
    the prediction set stays aligned with the instance set.
    """
    predictions: list[dict] = []
    results: list[GuardResult] = []
    model_name = str(getattr(adapter, "model_name", "autodev"))

    for index, instance in enumerate(instances):
        instance_id = str(instance.get("instance_id", f"inst_{index}"))
        attempt = _InstanceAttempt(
            adapter, instance, invoker, workdir_root, index, solve_fn
        )
        try:
            result = run_instance_with_quota_guard(
                instance_id,
                attempt,
                max_attempts=max_attempts,
                backoff=backoff,
                sleep=sleep,
                signal_tokens=signal_tokens,
                on_quota_wait=on_quota_wait,
            )
        except InstancePrepareError as exc:
            # Expected per-instance setup failure (e.g. an invalid base_commit):
            # record ERROR for THIS instance + a patch-less prediction so it is
            # accounted for, then CONTINUE the sweep. One bad instance must never
            # abort the whole overnight run. Any OTHER exception propagates (a
            # genuine harness bug) — the guard does not swallow it.
            results.append(
                GuardResult(
                    instance_id=instance_id,
                    status=ERROR,
                    outcome=None,
                    attempts=0,
                    quota_waits=0,
                    quota_wait_time_s=0.0,
                    detail=f"prepare failed: {exc}",
                    # fail_stdout_tail/fail_stderr_tail deliberately left at their
                    # "" defaults: prepare failed before any autodev subprocess
                    # ever ran, so there is genuinely nothing captured to surface
                    # here — not an oversight.
                )
            )
            predictions.append(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model_name,
                    "model_patch": "",
                }
            )
            continue
        results.append(result)

        if (
            result.status == COMPLETE
            and result.outcome is not None
            and result.workdir is not None
        ):
            # Feed the EXACT workdir the guard solved the winning attempt in — no
            # string reconstruction, so the scored patch can never desync.
            predictions.append(
                dict(adapter.predict(instance, result.workdir, result.outcome))
            )
        else:
            # Quota-exhausted (or no outcome): account for the instance with a
            # patch-less prediction — ERROR downstream, never a silent drop.
            predictions.append(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model_name,
                    "model_patch": "",
                }
            )

    return predictions, results


__all__ = [
    "COMPLETE",
    "DEFAULT_MAX_ATTEMPTS",
    "QUOTA_SIGNAL_TOKENS",
    "BackoffPolicy",
    "GuardResult",
    "QuotaWaitEvent",
    "SolveAttempt",
    "SolveFn",
    "SleepFn",
    "enforce_serial_subprocesses",
    "is_quota_abort",
    "run_guarded_solve",
    "run_instance_with_quota_guard",
]
