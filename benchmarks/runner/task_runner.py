"""Per-task execution loop for the AutoDev benchmark.

For each task this module:
  1. Copies ``task_dir/repo`` into a clean tmp working directory.
  2. ``git init`` + ``git add . && git commit -m initial`` (so the agent's
     diff can be computed via ``git diff HEAD``).
  3. Invokes the ``autodev`` CLI with ``init`` → ``plan --spec`` → ``execute``.
     Each command is bounded by a wall-clock timeout; the runner aborts and
     marks the task FAIL if any command exceeds it.
  4. Extracts the agent's final diff (preferring the autodev ledger; falling
     back to ``git diff HEAD``).
  5. Hands the diff to ``scorer.score_task_with_patch`` for verdict.
  6. Records per-task secondary metrics: wall-clock time, invocation count,
     diff size delta vs ground truth.

The autodev subprocess command is parameterisable via ``autodev_cmd`` so
unit tests can substitute a stub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .scorer import (
    diff_since_commit,
    extract_diff_from_ledger,
    iter_task_dirs,
    score_task_with_patch,
)

DEFAULT_TIMEOUT_SECONDS: int = 600  # per-autodev-command wall-clock cap
DEFAULT_TEST_TIMEOUT_SECONDS: int = 120


@dataclass
class TaskResult:
    """Outcome of running one benchmark task end-to-end."""

    task_id: str
    status: str  # "PASS" | "FAIL" | "ERROR"
    secondary: dict = field(default_factory=dict)
    error: str | None = None
    apply_error: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict:
        out: dict = {
            "task_id": self.task_id,
            "status": self.status,
            "secondary": dict(self.secondary),
        }
        if self.error:
            out["error"] = self.error
        if self.apply_error:
            out["apply_error"] = self.apply_error
        if self.stdout_tail:
            out["stdout_tail"] = self.stdout_tail
        if self.stderr_tail:
            out["stderr_tail"] = self.stderr_tail
        return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_tasks(tasks_root: Path) -> list[Path]:
    """Return all task directories under ``tasks_root`` in sorted order."""
    return list(iter_task_dirs(tasks_root))


def filter_tasks(all_tasks: Sequence[Path], selector: str | None) -> list[Path]:
    """Filter discovered task dirs by the CLI ``--task`` selector.

    ``"all"`` (default) returns everything; otherwise the selector is a
    comma-separated list of task IDs (directory basenames).
    """
    if selector is None or selector.strip().lower() == "all":
        return list(all_tasks)
    wanted = {part.strip() for part in selector.split(",") if part.strip()}
    return [t for t in all_tasks if t.name in wanted]


# ---------------------------------------------------------------------------
# Subprocess helper
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


def _init_git_repo(repo_dir: Path) -> str:
    """Initialise a git repo at the broken initial state and return its SHA.

    The returned SHA is the *baseline* the run is scored against: diffing it to
    the final worktree state recovers a fix even when AutoDev commits it (the
    P1 false-FAIL fix).
    """
    _git(["init", "-q", "-b", "main"], cwd=repo_dir)
    # Local-only identity so the commit succeeds in any environment.
    _git(["config", "user.email", "benchmark@autodev.local"], cwd=repo_dir)
    _git(["config", "user.name", "AutoDev Benchmark"], cwd=repo_dir)
    # F-5 fixture hygiene: seed a minimal, realistic ``.gitignore`` BEFORE the
    # bare ``git add .`` so regenerated bytecode is never committed into the
    # fixture's initial commit. Previously a fixture checkout that shipped
    # ``__pycache__/*.pyc`` (gitignored in AutoDev's own repo, hence invisible
    # to ``git status`` here) got those ``.pyc`` committed as TRACKED binary
    # files. pytest then regenerated them in the worktree during the QA gate,
    # and the worktree→main 3-way apply failed on the abbreviated binary hunk
    # — a spurious ``conflict_3way_failed`` that blocked DELIVER. Only append
    # the ignores when the fixture didn't already provide its own .gitignore,
    # so a task that intentionally tests gitignore behaviour is untouched.
    gitignore = repo_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    _git(["add", "."], cwd=repo_dir)
    _git(["commit", "-q", "-m", "initial"], cwd=repo_dir)
    head = _git(["rev-parse", "HEAD"], cwd=repo_dir)
    return head.stdout.strip() if head.returncode == 0 else "HEAD"


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _git_diff_head(repo_dir: Path) -> str:
    """Capture ``git diff HEAD`` for a repo (uncommitted + committed-since)."""
    res = _run(["git", "diff", "HEAD"], cwd=repo_dir, timeout=60)
    return res.stdout if res.returncode == 0 else ""


def _is_behavior_preserving(meta: dict) -> bool:
    """Return True if a task is behaviour-preserving (a refactor).

    Such tasks pass their ``test_command.sh`` with NO change, so they need a
    structural-change assertion to avoid a vacuous PASS (field finding P5).
    Recognised meta.json conventions (any one suffices):

    - ``"task_type": "refactor"`` (or ``"category": "refactor"``);
    - ``"behavior_preserving": true`` / ``"behaviour_preserving": true``.
    """
    type_keys = ("task_type", "category", "kind")
    for key in type_keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip().lower() == "refactor":
            return True
    for key in ("behavior_preserving", "behaviour_preserving"):
        if meta.get(key) is True:
            return True
    return False


def _diff_size(text: str) -> int:
    """Return the number of changed lines (additions + deletions) in a diff."""
    if not text:
        return 0
    n = 0
    for raw in text.splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+") or raw.startswith("-"):
            n += 1
    return n


# Benchmark hygiene: the lint QA gate is environment-fragile on minimal,
# config-less fixtures. ``qa/lint._run_eslint`` shells out to ``npx eslint .``
# which FETCHES ESLint v10 and hard-errors ("couldn't find eslint.config.js")
# on a repo with no lint config — a setup signal, not a code defect. The agent
# cannot fix that by editing source, so it drives an unwinnable retry loop ->
# block (Phase-4 P1 field finding). The benchmark scores DELIVERY via
# ``test_command.sh``; lint compliance is orthogonal and the WS-4 acceptance /
# field gates do not involve lint. Opt-in via ``AUTODEV_BENCH_DISABLE_LINT=1``
# (default OFF — committed behaviour unchanged).
_DISABLE_LINT_ENV = "AUTODEV_BENCH_DISABLE_LINT"


def _maybe_relax_env_fragile_gates(agent_repo: Path) -> None:
    """Turn off the lint QA gate in the freshly-``init``-ed config when
    ``AUTODEV_BENCH_DISABLE_LINT`` is set (best-effort; no-op otherwise)."""
    if not os.environ.get(_DISABLE_LINT_ENV):
        return
    cfg_path = agent_repo / ".autodev" / "config.json"
    if not cfg_path.is_file():
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    gates = cfg.get("qa_gates")
    if not isinstance(gates, dict):
        gates = {}
        cfg["qa_gates"] = gates
    gates["lint"] = False
    try:
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-task entry point
# ---------------------------------------------------------------------------


# Type for the autodev invoker — extracted so tests can stub it.
AutodevInvoker = Callable[[Sequence[str], Path, int], _SubprocessResult]


def _default_autodev_invoker(
    args: Sequence[str], cwd: Path, timeout: int
) -> _SubprocessResult:
    return _run(["autodev", *args], cwd=cwd, timeout=timeout)


def run_task(
    task_dir: Path,
    *,
    autodev_invoker: AutodevInvoker | None = None,
    autodev_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    test_timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    workdir_root: Path | None = None,
) -> TaskResult:
    """Run a single task end-to-end and return its result.

    ``autodev_invoker`` lets tests substitute a stub for the autodev CLI;
    None uses the real ``autodev`` binary on PATH.
    """
    invoker = autodev_invoker or _default_autodev_invoker
    task_id = task_dir.name
    meta_path = task_dir / "meta.json"
    spec_path = task_dir / "spec.md"

    if not meta_path.is_file():
        return TaskResult(task_id=task_id, status="ERROR", error="missing meta.json")
    if not spec_path.is_file():
        return TaskResult(task_id=task_id, status="ERROR", error="missing spec.md")

    invocations = 0
    cumulative_wall = 0.0

    cleanup_dir: Path | None = None
    try:
        if workdir_root is None:
            tmp_root = Path(tempfile.mkdtemp(prefix=f"bench-{task_id}-"))
            cleanup_dir = tmp_root
        else:
            tmp_root = workdir_root
            tmp_root.mkdir(parents=True, exist_ok=True)

        # Read meta to learn whether this is a behaviour-preserving (refactor)
        # task — those need a structural-change assertion, not test-only scoring.
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        require_structural_change = _is_behavior_preserving(meta)

        # Step 1: copy repo → fresh git repo. Capture the broken baseline commit
        # so a fix can be recovered even when AutoDev commits it (P1 fix).
        agent_repo = tmp_root / "agent_repo"
        if agent_repo.exists():
            shutil.rmtree(agent_repo)
        shutil.copytree(task_dir / "repo", agent_repo)
        initial_commit = _init_git_repo(agent_repo)

        # Step 2: invoke autodev init/plan/execute.
        #
        # ``plan`` takes the intent/spec as a POSITIONAL argument (click:
        # ``plan [OPTIONS] INTENT``); there is NO ``--spec`` flag — the spec
        # TEXT *is* the intent (the plan phase writes it to ``.autodev/spec.md``).
        # The historical ``["plan", "--spec", <path>]`` invocation made click
        # exit 2 (unknown option) in <1s, before any planning ran. We pass the
        # spec file's CONTENTS as the intent and ``--assume-defaults`` so headless
        # intake applies recommended defaults instead of hanging on a question.
        spec_text = spec_path.read_text(encoding="utf-8")
        autodev_failed_reason: str | None = None
        autodev_calls: list[tuple[str, int, float]] = []
        autodev_fail_stdout = ""
        autodev_fail_stderr = ""
        for args, label in (
            (["init"], "init"),
            (
                ["plan", spec_text, "--assume-defaults"],
                f"plan <spec:{len(spec_text)}c> --assume-defaults",
            ),
            (["execute"], "execute"),
        ):
            res = invoker(args, agent_repo, autodev_timeout_seconds)
            invocations += 1
            cumulative_wall += res.elapsed_seconds
            autodev_calls.append((label, res.returncode, round(res.elapsed_seconds, 3)))
            if res.timed_out:
                autodev_failed_reason = (
                    f"autodev {label} timed out after {autodev_timeout_seconds}s"
                )
                autodev_fail_stdout = res.stdout[-2000:]
                autodev_fail_stderr = res.stderr[-2000:]
                break
            if res.returncode != 0:
                autodev_failed_reason = f"autodev {label} exited {res.returncode}"
                autodev_fail_stdout = res.stdout[-2000:]
                autodev_fail_stderr = res.stderr[-2000:]
                break
            # init just wrote .autodev/config.json — optionally relax the
            # environment-fragile lint gate before plan/execute run.
            if label == "init":
                _maybe_relax_env_fragile_gates(agent_repo)

        # Step 3: extract the agent's diff.
        #
        # Precedence: an explicit ledger diff (when present) → the commit-based
        # initial→final diff (recovers a COMMITTED fix; the P1 fix) → a plain
        # `git diff HEAD` as a last resort. AutoDev commits its fixes, so the
        # commit-based diff is the load-bearing path; `git diff HEAD` alone
        # would false-FAIL every committed fix.
        ledger_diff = extract_diff_from_ledger(
            agent_repo / ".autodev" / "plan-ledger.jsonl"
        )
        commit_diff = diff_since_commit(agent_repo, initial_commit)
        agent_diff = ledger_diff or commit_diff or _git_diff_head(agent_repo)

        # Step 4: ground truth + diff size delta (best-effort metric).
        gt_path = task_dir / "ground_truth.patch"
        gt_size = _diff_size(gt_path.read_text(encoding="utf-8")) if gt_path.is_file() else 0
        agent_size = _diff_size(agent_diff)

        secondary = {
            "wall_time_s": round(cumulative_wall, 3),
            "invocations": invocations,
            "autodev_calls": autodev_calls,
            "diff_size_lines": agent_size,
            "ground_truth_diff_size_lines": gt_size,
            "diff_size_delta_lines": agent_size - gt_size,
            "behavior_preserving": require_structural_change,
        }
        # Surface the failing autodev command's output so a probe JSON is
        # self-diagnosing (no manual re-run needed to see WHY autodev failed).
        if autodev_fail_stdout:
            secondary["autodev_fail_stdout_tail"] = autodev_fail_stdout
        if autodev_fail_stderr:
            secondary["autodev_fail_stderr_tail"] = autodev_fail_stderr

        # Step 5: score in a fresh copy (so we don't conflate agent's
        # uncommitted changes with our patch application).
        score_dir = tmp_root / "score"
        score_dir.mkdir(parents=True, exist_ok=True)
        score = score_task_with_patch(
            task_dir,
            agent_diff,
            workdir=score_dir,
            test_timeout_seconds=test_timeout_seconds,
            require_structural_change=require_structural_change,
        )
        if score.structural_change is not None:
            secondary["structural_change"] = score.structural_change

        if score.passed:
            status = "PASS"
            error: str | None = autodev_failed_reason
        else:
            status = "FAIL"
            error = autodev_failed_reason

        return TaskResult(
            task_id=task_id,
            status=status,
            secondary=secondary,
            error=error,
            apply_error=score.apply_error,
            stdout_tail=score.stdout_tail,
            stderr_tail=score.stderr_tail,
        )
    except Exception as exc:  # pragma: no cover - defence in depth
        return TaskResult(task_id=task_id, status="ERROR", error=repr(exc))
    finally:
        if cleanup_dir is not None and cleanup_dir.is_dir():
            keep = os.environ.get("AUTODEV_BENCH_KEEP_WORKDIR")
            if not keep:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
