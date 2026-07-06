"""Shared solve-half foundation for AutoDev benchmarks (Phase-1 P1.1).

This module factors the *solve half* out of ``task_runner.run_task`` so the same,
exactly-preserved logic can be reused by the external SWE-bench-Lite adapter
(P1.2+) without duplicating the diff-recovery ladder or the autodev-driving loop.

The solve half is:

  1. drive AutoDev ``init`` -> ``plan <intent> --assume-defaults`` -> ``execute``
     via an injected, **env-carrying** :class:`SolveInvoker` (each command bounded
     by a per-command wall-clock timeout; abort on the first failure);
  2. after ``init`` writes ``.autodev/config.json``, deep-merge the profile's
     ``config_patch`` (the generalisation of the old lint-relax hook — e.g. turn
     ``test_runner`` off when arm64 deps fail, or force
     ``tournaments.max_parallel_subprocesses = 1`` to cut the within-task burst);
  3. recover the agent's change with the diff-recovery ladder
     ``ledger -> diff_since_commit(base) -> git diff HEAD`` (exactly the order and
     short-circuit ``run_task`` used).

``run_task`` delegates its core to :func:`solve` (adapting its legacy 3-arg
invoker to the env-carrying form); the external CLI calls :func:`solve` directly
with :func:`default_solve_invoker` (or a fake, in tests). The invoker abstraction
is what lets a caller inject the per-instance environment and a ``config_patch``
without ``solve`` knowing anything benchmark-specific.

The low-level subprocess primitives (``_SubprocessResult``, ``_run``, ``_git``,
``_git_diff_head``) live here — the shared foundation — and are re-exported by
``task_runner`` for backwards compatibility with the existing test-suite imports.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .scorer import (
    DEFAULT_EXCLUDED_DIRS,
    diff_since_commit,
    extract_diff_from_ledger,
)

# Tail size (chars) for a failing command's captured output (mirrors run_task).
_FAIL_OUTPUT_TAIL = 2000


# ---------------------------------------------------------------------------
# Low-level subprocess primitives (shared foundation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_seconds: float


def _run(
    cmd: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> _SubprocessResult:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired.stdout/.stderr are bytes even when text=True was
        # requested (Python captures them before decoding on timeout).  Decode
        # here so the result fields are always str and JSON-serialisable.
        def _decode(v: bytes | str | None) -> str:
            if isinstance(v, bytes):
                return v.decode("utf-8", errors="replace")
            return v or ""

        return _SubprocessResult(
            returncode=-1,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            timed_out=True,
            elapsed_seconds=time.perf_counter() - start,
        )
    return _SubprocessResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timed_out=False,
        elapsed_seconds=time.perf_counter() - start,
    )


def _git(args: Sequence[str], *, cwd: Path) -> _SubprocessResult:
    return _run(["git", *args], cwd=cwd, timeout=60)


def _git_diff_head(repo_dir: Path) -> str:
    """Capture ``git diff HEAD`` for a repo (uncommitted + committed-since)."""
    res = _run(["git", "diff", "HEAD"], cwd=repo_dir, timeout=60)
    return res.stdout if res.returncode == 0 else ""


def _rev_parse_head(repo_dir: Path) -> str:
    """Return the current HEAD SHA (the solve baseline), or ``"HEAD"`` on failure.

    Mirrors ``_init_git_repo``'s tail so the base the diff is taken against is
    identical to the value ``run_task`` previously captured from init.
    """
    head = _git(["rev-parse", "HEAD"], cwd=repo_dir)
    return head.stdout.strip() if head.returncode == 0 else "HEAD"


# ---------------------------------------------------------------------------
# config.json patching (generalises _maybe_relax_env_fragile_gates)
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, patch: Mapping) -> dict:
    """Recursively merge ``patch`` into ``base`` in place; nested dicts merge,
    scalars/lists overwrite. Returns ``base``."""
    for key, value in patch.items():
        existing = base.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            base[key] = value
    return base


def apply_config_patch(config_path: Path, patch: Mapping) -> None:
    """Deep-merge ``patch`` into an autodev ``config.json`` in place.

    A best-effort, idempotent generalisation of the old
    ``_maybe_relax_env_fragile_gates`` lint hook: it no-ops silently when the
    patch is empty, the config file is missing, or it cannot be read/parsed/
    written (exactly the failure tolerance the original had) and never raises.
    """
    if not patch:
        return
    if not config_path.is_file():
        return
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(cfg, dict):
        return
    _deep_merge(cfg, patch)
    try:
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Solve invoker + profile/outcome
# ---------------------------------------------------------------------------


class SolveInvoker(Protocol):
    """An env-carrying autodev invoker.

    Called once per autodev command (``init``/``plan``/``execute``). ``env`` is
    the full environment to run under (``None`` = inherit the parent process
    environment, matching the legacy no-overlay behaviour); ``cwd`` is the agent
    workdir; ``timeout`` is the per-command wall-clock cap in seconds. Returns a
    :class:`_SubprocessResult`.
    """

    def __call__(
        self,
        command_args: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        cwd: Path,
        timeout: int,
    ) -> _SubprocessResult: ...


def default_solve_invoker(
    command_args: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    cwd: Path,
    timeout: int,
) -> _SubprocessResult:
    """The real env-carrying invoker: shell out to the ``autodev`` CLI on PATH."""
    env_dict = dict(env) if env is not None else None
    return _run(["autodev", *command_args], cwd=cwd, timeout=timeout, env=env_dict)


@dataclass
class SolveProfile:
    """Intent-independent knobs for one solve attempt.

    - ``env``: overlay merged onto ``os.environ`` and handed to the invoker.
      Empty => the invoker is called with ``env=None`` (inherit), preserving the
      pre-refactor behaviour exactly.
    - ``config_patch``: deep-merged into ``.autodev/config.json`` right after
      ``init`` (e.g. ``{"qa_gates": {"test_runner": false}}`` when arm64 deps
      fail, or ``{"tournaments": {"max_parallel_subprocesses": 1}}`` to cut the
      within-task burst against the subscription cap).
    - ``timeout``: per-autodev-command wall-clock cap (seconds).
    - ``exclude_dirs``: pathspec exclusions for the recovered diff (the workdir/
      source-only diff policy). Defaults to ``.autodev/.claude/.cursor`` — the
      SWE-bench adapter widens this to also exclude test paths (P1.2).
    - ``max_attempts``: the attempt cap honoured by the Phase-1 quota-aware
      wrapper (P1.4) that RE-runs an instance after a quota abort. :func:`solve`
      itself performs exactly one attempt; this field is the contract the wrapper
      reads so the cap travels with the profile.
    """

    env: Mapping[str, str] = field(default_factory=dict)
    config_patch: Mapping = field(default_factory=dict)
    timeout: int = 600
    exclude_dirs: Sequence[str] = DEFAULT_EXCLUDED_DIRS
    max_attempts: int = 1


@dataclass
class SolveOutcome:
    """Result of one solve attempt.

    ``diff`` is the recovered patch text ("" when the solver produced nothing);
    ``empty_diff`` is the explicit "no change" flag the caller maps to FAIL/empty
    (never a silent PASS). ``success`` is True iff every autodev command exited 0
    (no timeout / non-zero). ``failed_reason`` carries the first failing command's
    summary (else ``None``). ``diff_source`` records which ladder rung produced
    the diff. The remaining fields are the raw exec bookkeeping ``run_task``
    stamps into ``secondary``.
    """

    diff: str
    base_sha: str
    success: bool
    empty_diff: bool
    diff_source: str  # "ledger" | "commit" | "worktree" | "none"
    ledger_path: Path
    failed_reason: str | None
    calls: list[tuple[str, int, float]]
    invocations: int
    wall_time_s: float
    fail_stdout_tail: str
    fail_stderr_tail: str


def _recover_diff(
    workdir: Path,
    base_sha: str,
    ledger_path: Path,
    exclude_dirs: Sequence[str],
) -> tuple[str, str]:
    """The diff-recovery ladder: ledger -> diff_since_commit(base) -> git diff HEAD.

    Preserves ``run_task``'s exact short-circuit (``ledger or commit or head``):
    each rung is consulted only if the previous produced nothing. Returns
    ``(diff, source)`` where ``source`` is the rung that produced it.
    """
    ledger_diff = extract_diff_from_ledger(ledger_path)
    if ledger_diff:
        return ledger_diff, "ledger"
    commit_diff = diff_since_commit(workdir, base_sha, exclude_dirs=exclude_dirs)
    if commit_diff:
        return commit_diff, "commit"
    head_diff = _git_diff_head(workdir)
    if head_diff:
        return head_diff, "worktree"
    return "", "none"


def solve(
    workdir: Path,
    intent: str,
    profile: SolveProfile,
    invoker: SolveInvoker,
) -> SolveOutcome:
    """Run the solve half on a prepared git ``workdir`` and recover the diff.

    ``workdir`` must already be a git repo checked out at its baseline commit
    (``run_task`` copies the fixture + ``_init_git_repo``; the SWE-bench adapter
    clones + checks out ``base_commit`` + inits). The baseline is read from
    ``HEAD`` here, so the recovered diff is relative to that baseline — which
    recovers a fix even when AutoDev *commits* it (the P1 false-FAIL fix).
    """
    base_sha = _rev_parse_head(workdir)

    effective_env: Mapping[str, str] | None
    if profile.env:
        # Merge overlay onto the parent env so autodev still sees PATH etc.
        effective_env = {**os.environ, **profile.env}
    else:
        effective_env = None  # inherit — legacy parity with _run(..., env=None)

    invocations = 0
    wall = 0.0
    calls: list[tuple[str, int, float]] = []
    failed_reason: str | None = None
    fail_stdout = ""
    fail_stderr = ""

    for args, label in (
        (["init"], "init"),
        (
            ["plan", intent, "--assume-defaults"],
            f"plan <spec:{len(intent)}c> --assume-defaults",
        ),
        (["execute"], "execute"),
    ):
        res = invoker(args, env=effective_env, cwd=workdir, timeout=profile.timeout)
        invocations += 1
        wall += res.elapsed_seconds
        calls.append((label, res.returncode, round(res.elapsed_seconds, 3)))
        if res.timed_out:
            failed_reason = f"autodev {label} timed out after {profile.timeout}s"
            fail_stdout = res.stdout[-_FAIL_OUTPUT_TAIL:]
            fail_stderr = res.stderr[-_FAIL_OUTPUT_TAIL:]
            break
        if res.returncode != 0:
            failed_reason = f"autodev {label} exited {res.returncode}"
            fail_stdout = res.stdout[-_FAIL_OUTPUT_TAIL:]
            fail_stderr = res.stderr[-_FAIL_OUTPUT_TAIL:]
            break
        # init just wrote .autodev/config.json — apply the config_patch before
        # plan/execute run.
        if label == "init":
            apply_config_patch(
                workdir / ".autodev" / "config.json", profile.config_patch
            )

    ledger_path = workdir / ".autodev" / "plan-ledger.jsonl"
    diff, diff_source = _recover_diff(
        workdir, base_sha, ledger_path, profile.exclude_dirs
    )
    empty = not (diff and diff.strip())

    return SolveOutcome(
        diff=diff,
        base_sha=base_sha,
        success=failed_reason is None,
        empty_diff=empty,
        diff_source=diff_source,
        ledger_path=ledger_path,
        failed_reason=failed_reason,
        calls=calls,
        invocations=invocations,
        wall_time_s=wall,
        fail_stdout_tail=fail_stdout,
        fail_stderr_tail=fail_stderr,
    )
