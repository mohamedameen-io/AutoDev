"""SWE-bench-Lite host-arm64 solve adapter (Phase-1 P1.2).

This is the benchmark-specific glue around the reusable solve-half
(:mod:`benchmarks.runner.solve`). It turns one SWE-bench-Lite *instance* into a
solvable git workdir, drives the arm64 per-instance environment decision, and
post-processes the solved workdir into a SWE-bench prediction record.

Behaviour (per the Phase-1 plan):

- **prepare**: materialise the instance ``repo`` into ``workdir`` (an injected
  cloner — real ``git clone`` in production), then the adapter itself
  ``git checkout``s ``base_commit`` and captures the resolved HEAD as the diff
  baseline (the P1.1 baseline-SHA pattern, so the source diff is measured from
  ``base_commit``). It then runs a best-effort per-instance arm64 venv install:

    * deps install OK  → keep ``qa_gates.test_runner`` ON (self-repair engages);
    * arm64 install FAILS → turn ``test_runner`` OFF via ``config_patch`` and
      solve "blind", recording the degradation on the instance report.

  Either way the ``config_patch`` cuts the within-task burst against the
  subscription cap with ``tournaments.max_parallel_subprocesses = 1`` and
  activates the execute-phase wall-budget guardrail with
  ``guardrails.execute_phase_wall_budget_s``, derived from the effective
  per-command timeout (``self.timeout``) so an operator ``--swebench-timeout``
  override can never invert the intended outer/inner ordering.

- **intent**: the instance ``problem_statement``.

- **predict**: recompute a **SOURCE-ONLY** diff from ``base_commit`` excluding
  scaffolding (``.autodev``/``.claude``/``.cursor``) **and the hidden test paths**
  (the model may not modify the tests the scorer applies). An EMPTY residual is
  marked **ERROR** (never a silent pass, never a capability FAIL); a non-empty
  residual is a **CANDIDATE** whose patch goes into the prediction record
  ``{instance_id, model_name_or_path, model_patch}``.

The clone and the venv-install are injectable so the whole adapter is
unit-testable with a tmp git repo and no network / no pip.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from benchmarks.adapters.base import Instance, InstancePrepareError
from benchmarks.runner.scorer import DEFAULT_EXCLUDED_DIRS, diff_since_commit
from benchmarks.runner.solve import (
    SolveOutcome,
    SolveProfile,
    _git,
    _rev_parse_head,
    _run,
)
from benchmarks.scorers.base import ERROR

# Default identity stamped into a prediction's ``model_name_or_path``.
DEFAULT_MODEL_NAME = "autodev"
# Per-autodev-command wall-clock cap for a SWE-bench solve (bigger than the v1
# fixtures — real repos + iterative test-repair take longer). Bumped 1800 -> 3600
# after a real Phase-1 pilot run: pallets__flask-4045 completed cleanly at 1617s
# and psf__requests-1963 errored (non-timeout) at 1257s, but django__django-10914
# was STILL doing real, productive work at the 1800s cutoff and got killed with
# zero usable result. Doubling to 3600s is evidence-conservative (only one data
# point says 1800s is insufficient) -- revisit after a full ~30-instance
# screening run produces more throughput data. Operator-overridable via
# ``--swebench-timeout`` (see ``benchmarks.runner.pilot._build_parser``), which
# defaults to ``None`` so this constant remains the fallback when the flag is
# omitted (see ``build_adapter`` below).
DEFAULT_SWEBENCH_TIMEOUT = 3600
# Code-review finding: the internal guardrail below MUST stay under whatever
# outer per-command timeout is actually in effect -- but that timeout is
# operator-overridable via ``--swebench-timeout``, so a bare module-level
# constant (e.g. a hardcoded ``3000``) can silently INVERT the ordering the
# moment an operator passes a small override (e.g. ``--swebench-timeout
# 1800``), reintroducing the exact opaque-SIGKILL behavior this task exists to
# prevent. ``prepare()`` therefore DERIVES the guardrail from ``self.timeout``
# (the effective, already-overridden value) via these two constants instead of
# a fixed number, so the ordering holds under any override:
#   execute_phase_wall_budget_s = min(max(self.timeout - MARGIN_S, FLOOR_S),
#                                      self.timeout - 1)
# The trailing ``min(..., self.timeout - 1)`` guarantees the budget stays
# STRICTLY under the effective timeout even for a tiny override where the
# floor itself would otherwise exceed it.
# With the default 3600s timeout this evaluates to exactly 3000s -- unchanged
# from the original hand-picked value.
_EXECUTE_PHASE_WALL_BUDGET_MARGIN_S = 600
# Never let an aggressively small --swebench-timeout collapse the internal
# guardrail to something degenerate (near-zero or, if timeout < margin,
# negative) -- ``subprocess.run(timeout=<negative>)`` and a near-instant
# guardrail trip are both nonsensical for real solve attempts.
_EXECUTE_PHASE_WALL_BUDGET_FLOOR_S = 300
# Internal orchestrator guardrail (``guardrails.execute_phase_wall_budget_s``,
# the core fix's DAG-wide execute-phase wall-clock ceiling) activated for this
# benchmark specifically -- production AutoDev users elsewhere are unaffected
# (the field is ``None``/off unless a config explicitly opts in). Fires FIRST
# with a clean, ledger-recorded ``execute_phase_wall_budget_exceeded`` stop --
# before the outer harness's raw subprocess-timeout kill would land as an
# opaque external SIGKILL. Deliberately does NOT also set
# ``impl_phase_wall_budget_s`` (the single-impl-tournament guardrail): the
# original incident was a multi-tournament chain across multiple tasks, not
# one runaway tournament, so that budget is unproven for this scenario -- add
# it later only if data shows a distinct single-tournament runaway.
# A source patch exists; PASS/FAIL is the *scorer's* verdict, not the adapter's.
CANDIDATE = "CANDIDATE"

# Real-op timeouts (never exercised by the unit gate — those ops are injected).
_CLONE_TIMEOUT = 900
_CHECKOUT_TIMEOUT = 120
_VENV_TIMEOUT = 300
_INSTALL_TIMEOUT = 1800

# Injection points: a cloner materialises ``repo`` into a workdir; an env
# installer returns True iff the per-instance deps installed on arm64.
Cloner = Callable[[str, Path], None]
EnvInstaller = Callable[[Instance, Path], bool]


class PrepareError(InstancePrepareError):
    """Raised when ``prepare`` cannot establish the correct diff baseline.

    The one case today: the ``git checkout <base_commit>`` failed, so HEAD is still
    at the clone default and the source diff would be measured from the WRONG base.
    Continuing would either fabricate a spurious huge patch (clone-default -> fix)
    or a false verdict, so ``prepare`` records an ``ERROR`` :class:`InstanceReport`
    and raises this instead of silently proceeding from the wrong HEAD.

    Subclasses the base-layer :class:`~benchmarks.adapters.base.InstancePrepareError`
    so the generic runner isolates it (ERROR-for-this-instance + continue the
    sweep) without importing this concrete adapter.
    """


@dataclass
class InstanceReport:
    """AutoDev's richer per-instance bookkeeping (distinct from the narrow
    SWE-bench prediction record).

    ``status`` is ``CANDIDATE`` (a source patch was produced, awaiting scoring) or
    ``ERROR`` (empty source residual / infra — never a silent pass). ``degraded_
    blind`` records whether the arm64 install failed and the solve ran with
    ``test_runner`` off. Consumed by the quota-aware wrapper (P1.4) and the coarse
    gate (P1.5).
    """

    instance_id: str
    status: str
    degraded_blind: bool
    base_commit: str
    wall_time_s: float = 0.0
    detail: str | None = None


@dataclass
class _PrepState:
    """Per-instance state captured in ``prepare`` and read back in ``predict``."""

    base_commit: str
    exclude_dirs: tuple[str, ...]
    test_paths: tuple[str, ...]
    degraded_blind: bool


# ---------------------------------------------------------------------------
# Test-path extraction (source-only diff exclusions)
# ---------------------------------------------------------------------------


def _paths_from_patch(patch_text: str) -> tuple[str, ...]:
    """Extract the file paths a unified (``git``) diff touches.

    Reads the ``--- a/<path>`` / ``+++ b/<path>`` headers, dropping ``/dev/null``
    (added/removed files). Used to derive the hidden test paths from an instance's
    ``test_patch`` so the source-only diff can exclude them.
    """
    paths: set[str] = set()
    for line in patch_text.splitlines():
        for prefix in ("+++ b/", "--- a/"):
            if line.startswith(prefix):
                candidate = line[len(prefix):].strip()
                # Strip a trailing tab-annotation ("path\t2024-..") if present.
                candidate = candidate.split("\t", 1)[0].strip()
                if candidate and candidate != "/dev/null":
                    paths.add(candidate)
    return tuple(sorted(paths))


def _test_paths(instance: Instance) -> tuple[str, ...]:
    """The test paths to exclude from the source-only diff.

    Prefers an explicit ``instance["test_paths"]`` (a list), else derives them
    from the instance's ``test_patch``.
    """
    explicit = instance.get("test_paths")
    if isinstance(explicit, (list, tuple)) and explicit:
        return tuple(str(p) for p in explicit)
    return _paths_from_patch(str(instance.get("test_patch") or ""))


# ---------------------------------------------------------------------------
# Default (real) clone + arm64 venv install — injectable; not hit by the gate.
# ---------------------------------------------------------------------------


def _default_clone(repo: str, workdir: Path) -> None:
    """Clone ``<repo>`` (``owner/name``) from GitHub into ``workdir``.

    ``git clone`` creates ``workdir``; the adapter checks out ``base_commit``
    afterwards. Best-effort: raises only if git itself fails to spawn.
    """
    url = f"https://github.com/{repo}.git"
    workdir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", url, str(workdir)], cwd=workdir.parent, timeout=_CLONE_TIMEOUT)


def _default_install_env(instance: Instance, workdir: Path) -> bool:
    """Best-effort per-instance arm64 venv + editable install.

    Returns True iff a venv is created and ``pip install -e .`` succeeds. Never
    raises — any failure (venv creation, resolution, native-build break on arm64,
    timeout) degrades to ``False`` so the caller solves blind. Per-version install
    specs (``environment_setup_commit``/``version``) are a pilot refinement (P1.6);
    the generic editable install is the honest Phase-1 default.
    """
    try:
        venv_dir = workdir / ".venv"
        made = _run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=workdir,
            timeout=_VENV_TIMEOUT,
        )
        if made.returncode != 0:
            return False
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        pip = venv_dir / bin_dir / "pip"
        installed = _run(
            [str(pip), "install", "-e", "."], cwd=workdir, timeout=_INSTALL_TIMEOUT
        )
        return installed.returncode == 0
    except Exception:  # noqa: BLE001 - best-effort; any failure => solve blind
        return False


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class SwebenchLiteAdapter:
    """Host-arm64 SWE-bench-Lite solve adapter (implements ``BenchmarkAdapter``)."""

    name = "swebench-lite-host-arm64"

    def __init__(
        self,
        *,
        cloner: Cloner = _default_clone,
        env_installer: EnvInstaller = _default_install_env,
        model_name: str = DEFAULT_MODEL_NAME,
        timeout: int = DEFAULT_SWEBENCH_TIMEOUT,
    ) -> None:
        self._clone = cloner
        self._install_env = env_installer
        self.model_name = model_name
        self.timeout = timeout
        # Public per-instance bookkeeping, appended once per ``predict``.
        self.reports: list[InstanceReport] = []
        self._prep: dict[str, _PrepState] = {}

    # -- prepare ------------------------------------------------------------

    def prepare(self, instance: Instance, workdir: Path) -> SolveProfile:
        instance_id = str(instance.get("instance_id", ""))

        # 1. Materialise the repo (injected clone; real ``git clone`` in prod).
        self._clone(str(instance["repo"]), workdir)

        # 2. Check out ``base_commit`` ourselves — the adapter owns this so the
        #    baseline is deterministic and testable. A FAILED checkout (invalid /
        #    missing base_commit) leaves HEAD at the clone default, so the source
        #    diff would be measured from the WRONG base — never proceed silently:
        #    record an ERROR InstanceReport and raise (base_sha is NOT captured
        #    from the wrong HEAD).
        base_commit = str(instance["base_commit"])
        checkout = _git(["checkout", "-q", base_commit], cwd=workdir)
        if checkout.returncode != 0:
            detail = (
                f"git checkout {base_commit} failed (exit {checkout.returncode}): "
                f"{(checkout.stderr or '').strip()[:200]}"
            )
            self.reports.append(
                InstanceReport(
                    instance_id=instance_id,
                    status=ERROR,
                    degraded_blind=False,
                    base_commit="",
                    wall_time_s=0.0,
                    detail=detail,
                )
            )
            raise PrepareError(detail)

        # 3. Baseline-SHA capture (P1.1 pattern): the resolved HEAD is the base the
        #    source diff is measured from (recovers a fix even if AutoDev commits).
        base_sha = _rev_parse_head(workdir)

        # 4. Best-effort per-instance arm64 venv. The install decision drives the
        #    per-instance ``test_runner`` policy.
        installed = bool(self._install_env(instance, workdir))
        degraded_blind = not installed

        # 5. config_patch: always cut the within-task burst AND activate the
        #    execute-phase wall-budget guardrail for this benchmark (see the
        #    _EXECUTE_PHASE_WALL_BUDGET_* constants above -- derived from
        #    self.timeout, the EFFECTIVE timeout including any
        #    --swebench-timeout override, so the ordering can't invert); turn
        #    ``test_runner`` OFF only when we cannot self-repair (blind solve).
        execute_phase_wall_budget_s = min(
            max(
                self.timeout - _EXECUTE_PHASE_WALL_BUDGET_MARGIN_S,
                _EXECUTE_PHASE_WALL_BUDGET_FLOOR_S,
            ),
            # Code-review finding (2nd round): the floor above protects
            # against a degenerate near-zero budget for realistic timeouts,
            # but for a SMALL ``--swebench-timeout`` override (below the
            # floor itself) it would otherwise let the floor exceed the
            # timeout it's supposed to stay under -- the exact inversion this
            # derivation exists to prevent. This outer ``min`` makes
            # ``budget < self.timeout`` strictly true for every value
            # ``build_adapter`` can ever produce (it already rejects
            # ``timeout <= 0``, so ``self.timeout - 1`` is never negative on
            # that path) at the cost of the floor being a soft target rather
            # than a hard guarantee for very small overrides (a regime with
            # no realistic benchmark use anyway). A prior version added an
            # extra ``max(..., 1)`` here "to be safe," but that silently
            # produced a tie (budget == timeout) at the unreachable T=1
            # boundary and didn't match this very comment -- removed rather
            # than special-cased, since ``build_adapter`` is the only
            # production entry point and already excludes non-positive
            # values.
            self.timeout - 1,
        )
        config_patch: dict[str, Any] = {
            "tournaments": {"max_parallel_subprocesses": 1},
            "guardrails": {
                "execute_phase_wall_budget_s": execute_phase_wall_budget_s
            },
        }
        if degraded_blind:
            config_patch["qa_gates"] = {"test_runner": False}

        # 6. SOURCE-ONLY diff exclusions: scaffolding + the hidden test paths.
        test_paths = _test_paths(instance)
        exclude_dirs = (*DEFAULT_EXCLUDED_DIRS, *test_paths)

        self._prep[instance_id] = _PrepState(
            base_commit=base_sha,
            exclude_dirs=exclude_dirs,
            test_paths=test_paths,
            degraded_blind=degraded_blind,
        )
        return SolveProfile(
            config_patch=config_patch,
            timeout=self.timeout,
            exclude_dirs=exclude_dirs,
        )

    # -- intent -------------------------------------------------------------

    def intent(self, instance: Instance) -> str:
        return str(instance["problem_statement"])

    # -- predict ------------------------------------------------------------

    def predict(
        self, instance: Instance, workdir: Path, outcome: SolveOutcome
    ) -> Mapping[str, Any]:
        instance_id = str(instance.get("instance_id", ""))
        prep = self._prep.get(instance_id)
        if prep is None:
            # Defensive: predict without a matching prepare (never via run_solve)
            # — recompute the baseline/exclusions rather than raising KeyError.
            base_commit = _rev_parse_head(workdir)
            exclude_dirs = (*DEFAULT_EXCLUDED_DIRS, *_test_paths(instance))
            degraded_blind = False
        else:
            base_commit = prep.base_commit
            exclude_dirs = prep.exclude_dirs
            degraded_blind = prep.degraded_blind

        # SOURCE-ONLY residual: base_commit -> worktree, minus scaffolding + tests.
        source_diff = diff_since_commit(
            workdir, base_commit, exclude_dirs=exclude_dirs
        )
        empty = not (source_diff and source_diff.strip())

        detail: str | None
        if empty:
            # Never a silent pass, never a capability FAIL: no source change is an
            # ERROR (only-tests-touched, no-op, or an autodev abort that landed
            # nothing). The scorer must not see it as a real attempt.
            status = ERROR
            model_patch = ""
            detail = outcome.failed_reason or "empty source-only residual"
        else:
            status = CANDIDATE
            model_patch = source_diff
            detail = outcome.failed_reason

        self.reports.append(
            InstanceReport(
                instance_id=instance_id,
                status=status,
                degraded_blind=degraded_blind,
                base_commit=base_commit,
                wall_time_s=round(outcome.wall_time_s, 3),
                detail=detail,
            )
        )
        return {
            "instance_id": instance_id,
            "model_name_or_path": self.model_name,
            "model_patch": model_patch,
        }


def build_adapter(args: Any) -> SwebenchLiteAdapter:
    """Construct the real (network-backed) adapter from CLI ``args``.

    Reads optional ``model_name`` / ``swebench_timeout`` attributes defensively so
    a bare ``argparse.Namespace`` (the external CLI's parser) works.

    Code-review finding: a non-positive ``--swebench-timeout`` (``0`` or
    negative) is rejected rather than silently accepted -- ``0`` would
    otherwise fall through the ``or DEFAULT_SWEBENCH_TIMEOUT`` fallback
    (0 is falsy) and mask the operator's mistake, while a negative value
    would thread straight into ``subprocess.run(timeout=<negative>)``.
    """
    model_name = getattr(args, "model_name", None) or DEFAULT_MODEL_NAME
    raw_timeout = getattr(args, "swebench_timeout", None)
    if raw_timeout is not None and int(raw_timeout) <= 0:
        raise ValueError(
            f"--swebench-timeout must be a positive number of seconds, "
            f"got {raw_timeout!r}"
        )
    timeout = raw_timeout or DEFAULT_SWEBENCH_TIMEOUT
    return SwebenchLiteAdapter(model_name=str(model_name), timeout=int(timeout))
