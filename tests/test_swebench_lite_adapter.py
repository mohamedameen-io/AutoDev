"""Gate tests for the SWE-bench-Lite host-arm64 solve adapter (Phase-1 P1.2).

These pin the four required proofs (a)-(d) from the plan
(``thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md``):

  (a) the emitted prediction record schema is EXACTLY
      ``{instance_id, model_name_or_path, model_patch}``;
  (b) a solved change that touches ONLY test paths leaves an EMPTY source-only
      residual and is marked ``ERROR`` — never a silent pass, never a FAIL that
      reads as a capability verdict;
  (c) ``base_commit`` is actually checked out (the workdir HEAD/baseline moves to
      it, and the worktree carries the base-state source, not a later commit);
  (d) a venv-install FAILURE flips ``qa_gates.test_runner`` OFF in the effective
      ``config_patch`` and RECORDS the blind degradation on the instance report,
      while an install SUCCESS keeps it on (the non-vacuous control).

Plus a small hermetic check of the dataset loader's JSONL degrade path (it must
not import ``datasets`` at module top level and must degrade when HF is absent).

Every test is hermetic + offline: the clone and the venv-install are INJECTED
(no network, no pip), the autodev invoker is an in-memory fake (no autodev, no
Claude), and the only real subprocess is local ``git`` on a tmp repo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from benchmarks.adapters.base import BenchmarkAdapter
from benchmarks.adapters.swebench_lite import (
    CANDIDATE,
    DEFAULT_SWEBENCH_TIMEOUT,
    InstallResult,
    SwebenchLiteAdapter,
    _EXECUTE_PHASE_WALL_BUDGET_FLOOR_S,
    _EXECUTE_PHASE_WALL_BUDGET_MARGIN_S,
    _FAIL_OUTPUT_TAIL,
    build_adapter,
)
from benchmarks.datasets import swebench_lite as ds
from benchmarks.runner.solve import (
    _git,
    _rev_parse_head,
    solve,
)
from benchmarks.scorers.base import ERROR

# The default-timeout-derived guardrail value, computed the SAME way
# production code derives it (not a hardcoded re-pin) -- code-review finding:
# the value must track ``self.timeout`` (the effective, possibly
# ``--swebench-timeout``-overridden timeout), not a bare module constant, or
# an operator override can silently invert the outer/inner ordering.
_DEFAULT_EXECUTE_PHASE_WALL_BUDGET_S = max(
    DEFAULT_SWEBENCH_TIMEOUT - _EXECUTE_PHASE_WALL_BUDGET_MARGIN_S,
    _EXECUTE_PHASE_WALL_BUDGET_FLOOR_S,
)

# ---------------------------------------------------------------------------
# Fixture content: a tiny repo with a source module and a test module.
# ---------------------------------------------------------------------------

BASE_SRC = "def f():\n    return 1\n"
LATER_SRC = "def f():\n    return 99\n"  # a divergent later commit
FIXED_SRC = "def f():\n    return 2\n"  # what a "solver" produces
BASE_TEST = "def test_f():\n    assert f() == 1\n"

# A SWE-bench-style test_patch: the file paths it touches are the TEST paths the
# source-only diff must exclude (the model may not modify the hidden tests).
TEST_PATCH = (
    "diff --git a/tests/test_mod.py b/tests/test_mod.py\n"
    "--- a/tests/test_mod.py\n"
    "+++ b/tests/test_mod.py\n"
    "@@ -1,1 +1,2 @@\n"
    " def test_f():\n"
    "+    assert f() == 2\n"
)


def _make_instance(base_commit: str = "base-ref") -> dict:
    return {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "base_commit": base_commit,
        "problem_statement": "make f return 2",
        "test_patch": TEST_PATCH,
        "version": "1.0",
        "environment_setup_commit": "envsha",
    }


def _fake_cloner(repo: str, workdir: Path) -> None:
    """Materialise ``workdir`` as a git repo whose ``base-ref`` tag holds the base
    state, then advance HEAD to a DIVERGENT later commit.

    This forces the adapter's own ``git checkout base_commit`` to genuinely move
    HEAD back to the base (gate c). No network — this stands in for ``git clone``.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=workdir)
    _git(["config", "user.email", "t@t"], cwd=workdir)
    _git(["config", "user.name", "t"], cwd=workdir)
    (workdir / "mod.py").write_text(BASE_SRC, encoding="utf-8")
    tests_dir = workdir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_mod.py").write_text(BASE_TEST, encoding="utf-8")
    _git(["add", "."], cwd=workdir)
    _git(["commit", "-qm", "base"], cwd=workdir)
    _git(["tag", "base-ref"], cwd=workdir)
    # Advance HEAD so a no-op checkout can't accidentally satisfy gate (c).
    (workdir / "mod.py").write_text(LATER_SRC, encoding="utf-8")
    _git(["add", "."], cwd=workdir)
    _git(["commit", "-qm", "later"], cwd=workdir)


def _ok_result(elapsed: float = 0.0):
    from benchmarks.runner.solve import _SubprocessResult

    return _SubprocessResult(
        returncode=0, stdout="", stderr="", timed_out=False, elapsed_seconds=elapsed
    )


def _writing_invoker(writes: dict[str, str]):
    """An autodev invoker fake that, on ``execute``, writes ``relpath -> content``
    into the workdir (an uncommitted change, exactly like a solver leaving dirty
    edits). ``init``/``plan`` are no-ops that exit 0."""

    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            for rel, content in writes.items():
                dest = Path(cwd) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
        return _ok_result()

    return invoker


def _committing_invoker(writes: dict[str, str]):
    """Like ``_writing_invoker``, but also ``git add`` + ``git commit``s the
    written files on ``execute`` -- mirrors what real AutoDev actually does (see
    the module docstring: "AutoDev *commits* its fix").

    Load-bearing distinction, verified empirically: an UNTRACKED file (as
    ``_writing_invoker`` leaves it) is invisible to ``git diff <base_commit>``
    regardless of any pathspec exclusion -- so a test that only wants to prove a
    scaffolding dir is excluded from the source-only diff must first make the
    file TRACKED, or it passes vacuously (ERROR for the wrong reason: nothing
    tracked changed at all, not because the pathspec excluded it)."""

    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            for rel, content in writes.items():
                dest = Path(cwd) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            _git(["add", "-A"], cwd=Path(cwd))
            _git(["commit", "-qm", "autodev scaffolding"], cwd=Path(cwd))
        return _ok_result()

    return invoker


def _adapter(**kwargs) -> SwebenchLiteAdapter:
    """Adapter wired with the fake cloner + a install-OK stub, unless overridden."""
    kwargs.setdefault("cloner", _fake_cloner)
    kwargs.setdefault(
        "env_installer", lambda instance, workdir: InstallResult(installed=True)
    )
    return SwebenchLiteAdapter(**kwargs)


# ---------------------------------------------------------------------------
# Gate (a): prediction record schema is exactly the SWE-bench triple.
# ---------------------------------------------------------------------------


def test_prediction_record_schema(tmp_path: Path):
    """A solved source change yields a prediction record whose keys are EXACTLY
    ``{instance_id, model_name_or_path, model_patch}`` (no more, no less), with the
    instance id echoed and the source change carried in ``model_patch``."""
    adapter = _adapter(model_name="autodev-x")
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({"mod.py": FIXED_SRC}))
    pred = adapter.predict(instance, workdir, outcome)

    assert set(pred.keys()) == {"instance_id", "model_name_or_path", "model_patch"}
    assert pred["instance_id"] == "demo__repo-1"
    assert pred["model_name_or_path"] == "autodev-x"
    assert "return 2" in pred["model_patch"]
    # Non-vacuous: a genuine source change is a CANDIDATE (not ERROR).
    assert adapter.reports[-1].status == CANDIDATE


# ---------------------------------------------------------------------------
# Gate (b): a test-only change -> empty source residual -> ERROR.
# ---------------------------------------------------------------------------


def test_test_only_change_is_error_not_silent_pass(tmp_path: Path):
    """When the solver touches ONLY a declared test path, the SOURCE-ONLY residual
    is empty. That must be marked ERROR (infra/no-op), never a silent pass and
    never a FAIL-as-capability-verdict. The emitted ``model_patch`` is empty."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    # Change ONLY the hidden test file (a declared test path).
    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({"tests/test_mod.py": BASE_TEST + "    # edit\n"}))
    pred = adapter.predict(instance, workdir, outcome)

    report = adapter.reports[-1]
    assert report.status == ERROR, "test-only change must be ERROR, not a pass/FAIL"
    assert report.status != CANDIDATE
    assert pred["model_patch"] == ""
    # Still a well-formed record (schema holds even for ERROR instances).
    assert set(pred.keys()) == {"instance_id", "model_name_or_path", "model_patch"}


def test_test_path_exclusion_is_load_bearing(tmp_path: Path):
    """Non-vacuous control for gate (b): the SAME edit to ``tests/test_mod.py`` is
    a CANDIDATE when the instance declares NO test paths (nothing to exclude), and
    an ERROR only when it does. This proves the ERROR is caused by the test-path
    exclusion -- not by test files being empty by accident."""
    adapter = _adapter()
    # Instance with an EMPTY test_patch => no test paths are excluded.
    instance = _make_instance()
    instance["test_patch"] = ""
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({"tests/test_mod.py": BASE_TEST + "    # edit\n"}))
    pred = adapter.predict(instance, workdir, outcome)

    assert adapter.reports[-1].status == CANDIDATE
    assert pred["model_patch"] != ""


def test_no_change_at_all_is_error(tmp_path: Path):
    """A null solver (no edits anywhere) -> empty residual -> ERROR (never a silent
    pass). The non-vacuous counterpart to the CANDIDATE case in gate (a)."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    outcome = solve(workdir, adapter.intent(instance), profile, _writing_invoker({}))
    pred = adapter.predict(instance, workdir, outcome)

    assert adapter.reports[-1].status == ERROR
    assert pred["model_patch"] == ""


def test_autodev_scaffolding_excluded_from_source_patch(tmp_path: Path):
    """A change confined to ``.autodev/`` scaffolding is not a source fix: the
    source-only residual excludes it -> ERROR. Proves the ``.autodev`` exclusion
    (reused from ``diff_since_commit``) is live in the adapter's residual."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({".autodev/scratch.txt": "agent notes\n"}))
    pred = adapter.predict(instance, workdir, outcome)

    assert adapter.reports[-1].status == ERROR
    assert pred["model_patch"] == ""


def test_bench_scaffolding_excluded_from_source_patch(tmp_path: Path):
    """A change confined to ``.autodev-bench/`` -- this adapter's OWN scaffolding
    dir for the install-failure log, a SIBLING of ``.autodev/`` (see
    ``_default_install_env``) -- is not a source fix either: the source-only
    residual excludes it -> ERROR.

    Uses ``_committing_invoker`` (NOT ``_writing_invoker``) so the scaffolding
    file is actually TRACKED (git-added + committed, mirroring what real
    AutoDev does) before the diff is taken: an untracked file is invisible to
    ``git diff <base_commit>`` regardless of any pathspec exclusion (verified
    empirically), so committing first is what makes this a genuinely
    non-vacuous proof that the ``.autodev-bench`` pathspec exclusion itself is
    load-bearing -- confirmed by reverting ``_ADAPTER_EXCLUDED_DIRS`` to omit
    ``.autodev-bench`` and observing this exact test fail (CANDIDATE, not
    ERROR) before restoring the fix."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    outcome = solve(
        workdir,
        adapter.intent(instance),
        profile,
        _committing_invoker({".autodev-bench/install-failure.log": "boom\n"}),
    )
    pred = adapter.predict(instance, workdir, outcome)

    assert adapter.reports[-1].status == ERROR
    assert pred["model_patch"] == ""


# ---------------------------------------------------------------------------
# Gate (c): base_commit is actually checked out.
# ---------------------------------------------------------------------------


def test_base_commit_is_checked_out(tmp_path: Path):
    """After ``prepare``, the workdir HEAD is the ``base_commit`` and the worktree
    carries base-state source (BASE_SRC), NOT the divergent later commit. Proves
    the adapter's own checkout moved HEAD from ``later`` back to base."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    adapter.prepare(instance, workdir)

    base_ref_sha = _git(["rev-parse", "base-ref"], cwd=workdir).stdout.strip()
    head_sha = _rev_parse_head(workdir)
    assert head_sha == base_ref_sha, "HEAD was not moved to base_commit"
    # The worktree must be the BASE state, not the later divergent commit.
    assert (workdir / "mod.py").read_text(encoding="utf-8") == BASE_SRC
    assert (workdir / "mod.py").read_text(encoding="utf-8") != LATER_SRC


def test_checkout_failure_is_error_not_silent_proceed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A FAILED ``git checkout <base_commit>`` must NOT be silently ignored.

    With the checkout returning non-zero (invalid/missing base_commit), the adapter
    must record an ERROR InstanceReport and raise ``PrepareError`` rather than
    proceeding from the clone-default HEAD (which would measure the diff from the
    WRONG base). Crucially, ``base_sha`` is NOT captured from the wrong HEAD.

    Non-vacuous: before the fix the returncode was ignored, so ``prepare`` returned
    a profile with a base captured from the (wrong) 'later' commit and NO exception
    — this ``pytest.raises`` would then fail (DID NOT RAISE)."""
    from benchmarks.adapters import swebench_lite as adp
    from benchmarks.runner.solve import _SubprocessResult

    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    real_git = adp._git

    def failing_git(args, *, cwd):
        # Fail ONLY the base_commit checkout; everything else behaves normally.
        if args and args[0] == "checkout":
            return _SubprocessResult(
                returncode=1,
                stdout="",
                stderr="fatal: reference is not a tree: base-ref",
                timed_out=False,
                elapsed_seconds=0.0,
            )
        return real_git(args, cwd=cwd)

    monkeypatch.setattr(adp, "_git", failing_git)

    with pytest.raises(adp.PrepareError):
        adapter.prepare(instance, workdir)

    # Recorded as ERROR (never a silent proceed, never a false PASS)...
    assert adapter.reports, "a failed checkout must still record an InstanceReport"
    assert adapter.reports[-1].status == ERROR
    # ...and the base was NOT captured from the wrong (clone-default) HEAD.
    assert adapter.reports[-1].base_commit == ""


def test_diff_is_measured_from_base_commit(tmp_path: Path):
    """The source patch is measured from ``base_commit``: a fix to ``mod.py`` shows
    up as a change relative to BASE_SRC (removing ``return 1``, adding ``return
    2``). If the adapter diffed from the later commit instead, the hunk would be
    wrong -- so this ties the emitted patch to the checked-out base."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)
    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({"mod.py": FIXED_SRC}))
    pred = adapter.predict(instance, workdir, outcome)

    patch = pred["model_patch"]
    assert "-    return 1" in patch, patch
    assert "+    return 2" in patch, patch
    assert "return 99" not in patch, "patch leaked the later (non-base) commit"


# ---------------------------------------------------------------------------
# Gate (d): venv-install failure flips test_runner off + records blind.
# ---------------------------------------------------------------------------


def test_venv_install_failure_flips_test_runner_off_and_records_blind(tmp_path: Path):
    """When the arm64 venv install FAILS, the effective ``config_patch`` turns
    ``qa_gates.test_runner`` OFF (solve blind, no self-repair) AND the instance
    report records ``degraded_blind=True`` -- while still cutting the burst with
    ``max_parallel_subprocesses=1`` and activating the execute-phase wall-budget
    guardrail. End-to-end, the patch flips real config state (deep-merged,
    siblings preserved).

    Also pins the install-failure tail plumbing: the ``InstallResult`` tails
    returned by the injected ``env_installer`` must flow through ``_PrepState``
    into the final ``InstanceReport`` -- exactly like ``degraded_blind`` already
    does -- so a pilot run can surface WHY an instance went blind without
    re-running the install by hand."""
    adapter = _adapter(
        env_installer=lambda instance, workdir: InstallResult(  # install FAILS
            installed=False,
            stdout_tail="scripted venv/pip stdout tail",
            stderr_tail="scripted venv/pip stderr tail",
        )
    )
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)

    # (1) the effective config_patch on the profile is degraded blind.
    assert profile.config_patch["qa_gates"]["test_runner"] is False
    assert profile.config_patch["tournaments"]["max_parallel_subprocesses"] == 1
    assert (
        profile.config_patch["guardrails"]["execute_phase_wall_budget_s"]
        == _DEFAULT_EXECUTE_PHASE_WALL_BUDGET_S
    )

    # (2) end-to-end: an autodev init that writes a config.json gets the patch
    #     deep-merged (test_runner flipped, unrelated siblings preserved).
    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "init":
            cfg_dir = Path(cwd) / ".autodev"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(
                json.dumps({
                    "qa_gates": {"test_runner": True, "lint": True},
                    "tournaments": {"max_parallel_subprocesses": 8},
                    "guardrails": {"max_duration_s_per_task": 300},
                }),
                encoding="utf-8",
            )
        elif args and args[0] == "execute":
            (Path(cwd) / "mod.py").write_text(FIXED_SRC, encoding="utf-8")
        return _ok_result()

    outcome = solve(workdir, adapter.intent(instance), profile, invoker)
    adapter.predict(instance, workdir, outcome)

    cfg = json.loads((workdir / ".autodev" / "config.json").read_text(encoding="utf-8"))
    assert cfg["qa_gates"]["test_runner"] is False  # flipped by the patch
    assert cfg["qa_gates"]["lint"] is True  # deep-merge preserved sibling
    assert cfg["tournaments"]["max_parallel_subprocesses"] == 1
    assert cfg["guardrails"]["execute_phase_wall_budget_s"] == _DEFAULT_EXECUTE_PHASE_WALL_BUDGET_S
    assert cfg["guardrails"]["max_duration_s_per_task"] == 300  # sibling preserved

    # (3) the blind degradation is recorded on the report.
    assert adapter.reports[-1].degraded_blind is True
    # (4) the install-failure tails are threaded onto the report too.
    assert adapter.reports[-1].install_stdout_tail == "scripted venv/pip stdout tail"
    assert adapter.reports[-1].install_stderr_tail == "scripted venv/pip stderr tail"


def test_venv_install_success_keeps_test_runner_on(tmp_path: Path):
    """Non-vacuous control for gate (d): when the install SUCCEEDS, the config_patch
    does NOT force ``test_runner`` off (self-repair stays engaged) and the report is
    not blind -- but the burst cut and the wall-budget guardrail still apply.

    Also the non-vacuous control for the install-failure tail plumbing: a
    successful install must leave the report's tails EMPTY (proves the tails are
    genuinely sourced from the ``InstallResult``, not always-populated
    boilerplate)."""
    adapter = _adapter(
        env_installer=lambda instance, workdir: InstallResult(installed=True)
    )  # install OK
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)

    assert "qa_gates" not in profile.config_patch, (
        "install-OK must not force test_runner off"
    )
    assert profile.config_patch["tournaments"]["max_parallel_subprocesses"] == 1
    assert (
        profile.config_patch["guardrails"]["execute_phase_wall_budget_s"]
        == _DEFAULT_EXECUTE_PHASE_WALL_BUDGET_S
    )

    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({"mod.py": FIXED_SRC}))
    adapter.predict(instance, workdir, outcome)
    assert adapter.reports[-1].degraded_blind is False
    assert adapter.reports[-1].install_stdout_tail == ""
    assert adapter.reports[-1].install_stderr_tail == ""


# ---------------------------------------------------------------------------
# _default_install_env: the REAL (non-injected) installer must itself capture
# output on failure -- confirmed empirically via both Phase-1 pilot reports
# showing EMPTY tails for the two blind instances (the bug this workstream
# fixes). These tests script ``_run`` directly so no real venv/pip subprocess
# ever runs (hermetic, fast) while still exercising the production function.
# ---------------------------------------------------------------------------


def test_default_install_env_captures_tail_and_writes_full_log_on_pip_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A scripted, REAL ``_default_install_env`` failure (not the injected
    ``env_installer`` seam) must populate BOTH the returned ``InstallResult``
    tails AND persist the FULL (untruncated) output to a durable on-disk log at
    ``workdir/.autodev-bench/install-failure.log`` -- a SIBLING of ``.autodev/``,
    never nested under it (``autodev init`` refuses to run if ``.autodev/``
    already exists, and this installer runs BEFORE ``init`` ever does)."""
    from benchmarks.adapters import swebench_lite as adp
    from benchmarks.runner.solve import _SubprocessResult

    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)

    long_stdout = "resolving dependencies...\n" * 200
    long_stderr = "error: could not build wheels for native-ext\n" * 200
    assert len(long_stdout) > _FAIL_OUTPUT_TAIL
    assert len(long_stderr) > _FAIL_OUTPUT_TAIL

    def fake_run(cmd, *, cwd, timeout, env=None):
        if "venv" in cmd:  # `sys.executable -m venv <dir>` succeeds
            return _SubprocessResult(
                returncode=0, stdout="", stderr="", timed_out=False, elapsed_seconds=0.01
            )
        # `<venv>/bin/pip install -e .` FAILS -- the realistic arm64 failure mode.
        return _SubprocessResult(
            returncode=1,
            stdout=long_stdout,
            stderr=long_stderr,
            timed_out=False,
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr(adp, "_run", fake_run)

    result = adp._default_install_env({"instance_id": "demo__1"}, workdir)

    assert isinstance(result, adp.InstallResult)
    assert result.installed is False
    # Tail convention mirrors SolveOutcome.fail_stdout_tail/fail_stderr_tail:
    # the LAST _FAIL_OUTPUT_TAIL chars, not the full text.
    assert result.stdout_tail == long_stdout[-_FAIL_OUTPUT_TAIL:]
    assert result.stderr_tail == long_stderr[-_FAIL_OUTPUT_TAIL:]
    assert len(result.stdout_tail) == _FAIL_OUTPUT_TAIL
    assert len(result.stderr_tail) == _FAIL_OUTPUT_TAIL

    # The FULL output is durably persisted to the sibling scaffolding dir.
    log_path = workdir / ".autodev-bench" / "install-failure.log"
    assert log_path.is_file(), "install-failure.log was not written on failure"
    log_text = log_path.read_text(encoding="utf-8")
    assert long_stdout in log_text, "full stdout must be in the on-disk log"
    assert long_stderr in log_text, "full stderr must be in the on-disk log"
    # Never created (or nested under) .autodev/ itself.
    assert not (workdir / ".autodev").exists()


def test_default_install_env_success_leaves_no_tail_and_no_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Broken-control proving the fix is load-bearing: on a SUCCESSFUL install,
    ``_default_install_env`` must return EMPTY tails and must NOT write
    ``install-failure.log`` at all. If the capture/log-write were unconditional
    (always firing, not genuinely gated on failure), this control would catch it
    -- the log file would exist / the tails would be non-empty even here."""
    from benchmarks.adapters import swebench_lite as adp
    from benchmarks.runner.solve import _SubprocessResult

    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)

    def fake_run(cmd, *, cwd, timeout, env=None):
        return _SubprocessResult(
            returncode=0,
            stdout="some benign venv/pip chatter\n",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr(adp, "_run", fake_run)

    result = adp._default_install_env({"instance_id": "demo__1"}, workdir)

    assert result.installed is True
    assert result.stdout_tail == ""
    assert result.stderr_tail == ""
    assert not (workdir / ".autodev-bench").exists(), (
        "no scaffolding/log should be written when the install succeeds"
    )


def test_default_install_env_exception_path_returns_install_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The pre-existing catch-all (``except Exception``) must still degrade to a
    non-raising, well-typed result under the new return type -- and still make a
    best-effort attempt to leave a diagnostic trail (the exception text) rather
    than silently returning nothing, as the old bare ``False`` did."""
    from benchmarks.adapters import swebench_lite as adp

    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)

    def raising_run(cmd, *, cwd, timeout, env=None):
        raise RuntimeError("venv module unavailable")

    monkeypatch.setattr(adp, "_run", raising_run)

    result = adp._default_install_env({"instance_id": "demo__1"}, workdir)

    assert isinstance(result, adp.InstallResult)
    assert result.installed is False
    assert "venv module unavailable" in result.stderr_tail


def test_write_install_failure_log_swallows_mkdir_failure(tmp_path: Path) -> None:
    """WS7 #2 (control): a pre-existing FILE where the ``.autodev-bench``
    scaffolding DIR would go makes ``mkdir(parents=True, exist_ok=True)`` raise
    (``FileExistsError``). ``_write_install_failure_log`` is strictly
    best-effort and must swallow it — never raise. (This case is a
    ``FileExistsError`` = ``OSError``, so it is caught by the pre-fix
    ``except`` too; it is the non-vacuous control for the sibling below.)"""
    from benchmarks.adapters import swebench_lite as adp

    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)
    # Occupy the scaffolding path with a FILE so mkdir cannot create the dir.
    (workdir / adp._BENCH_SCAFFOLD_DIRNAME).write_text("i am a file", encoding="utf-8")

    # Must not raise (and must not clobber the pre-existing file).
    adp._write_install_failure_log(workdir, stdout="out", stderr="err")
    assert (workdir / adp._BENCH_SCAFFOLD_DIRNAME).is_file()


def test_default_install_env_never_escalates_when_log_write_raises_non_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WS7 #1 (BUG, red-before-green): ``_write_install_failure_log`` is called
    from INSIDE ``_default_install_env``'s catch-all ``except Exception`` branch,
    where ``str(exc)`` can embed a surrogate-escaped path so the log-write's own
    ``write_text`` raises ``UnicodeEncodeError`` — NOT an ``OSError``. The pre-fix
    ``except OSError`` let that propagate straight out of ``_default_install_env``
    (and thus out of ``prepare()``), aborting the whole unattended sweep.

    With the fix (``except Exception``) the log-write failure is swallowed and a
    well-typed ``InstallResult`` is returned. RED before the fix (the
    ``UnicodeEncodeError`` escapes), GREEN after."""
    from benchmarks.adapters import swebench_lite as adp
    from benchmarks.runner.solve import _SubprocessResult

    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)

    # The install FAILS so a log write is genuinely attempted.
    def fake_run(cmd, *, cwd, timeout, env=None):
        return _SubprocessResult(
            returncode=1, stdout="out", stderr="err", timed_out=False, elapsed_seconds=0.01
        )

    monkeypatch.setattr(adp, "_run", fake_run)

    # The log-write's write_text raises a NON-OSError (the surrogate-escape
    # UnicodeEncodeError the docstring/bug is about).
    def boom_write_text(self, *args, **kwargs):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(Path, "write_text", boom_write_text)

    # Must NOT raise — degrades to a well-typed InstallResult(installed=False).
    result = adp._default_install_env(
        {"instance_id": "demo__1", "repo": "some/unknown"}, workdir
    )
    assert isinstance(result, adp.InstallResult)
    assert result.installed is False


def test_config_patch_activates_execute_phase_wall_budget_not_impl_budget(
    tmp_path: Path,
):
    """Dedicated pin for the benchmark wiring: ``prepare``'s ``config_patch`` sets
    ``guardrails.execute_phase_wall_budget_s`` (comfortably under the outer
    ``DEFAULT_SWEBENCH_TIMEOUT`` -- 3000s at the default 3600s timeout), nested
    alongside the pre-existing ``tournaments.max_parallel_subprocesses = 1``
    burst cut -- and must NOT also set ``impl_phase_wall_budget_s`` (that
    guardrail is unproven for this multi-task-chain incident shape and is
    deliberately left for later data)."""
    adapter = _adapter()
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)

    assert profile.config_patch["guardrails"] == {
        "execute_phase_wall_budget_s": _DEFAULT_EXECUTE_PHASE_WALL_BUDGET_S
    }
    assert _DEFAULT_EXECUTE_PHASE_WALL_BUDGET_S == 3000  # unchanged from before
    assert profile.config_patch["tournaments"] == {"max_parallel_subprocesses": 1}
    assert "impl_phase_wall_budget_s" not in profile.config_patch["guardrails"]


def test_execute_phase_wall_budget_tracks_swebench_timeout_override(
    tmp_path: Path,
):
    """Code-review regression: the guardrail budget must be DERIVED from the
    effective (possibly ``--swebench-timeout``-overridden) timeout, not a fixed
    module constant -- otherwise a small override can silently invert the
    intended outer/inner ordering and reintroduce the opaque-SIGKILL behavior
    this whole feature exists to prevent."""
    # A large override: the derived budget scales up with it (margin below).
    big = _adapter(timeout=10_000)
    profile = big.prepare(_make_instance(), tmp_path / "big")
    assert (
        profile.config_patch["guardrails"]["execute_phase_wall_budget_s"]
        == 10_000 - _EXECUTE_PHASE_WALL_BUDGET_MARGIN_S
    )

    # A small override (below the floor itself): the invariant-preserving
    # outer clamp wins over the floor -- the budget must stay STRICTLY under
    # the effective timeout even here, never negative or degenerate. This is
    # the exact scenario a first attempt at this derivation got wrong (the
    # floor alone would have produced 300 > 100, inverting the ordering).
    small = _adapter(timeout=100)
    profile = small.prepare(_make_instance(), tmp_path / "small")
    budget = profile.config_patch["guardrails"]["execute_phase_wall_budget_s"]
    assert budget == 99
    assert budget < 100, "the guardrail must still fire before the outer timeout"


def test_build_adapter_rejects_non_positive_swebench_timeout():
    """Code-review finding: ``--swebench-timeout 0`` must NOT silently fall
    back to the default (0 is falsy, so a naive ``or DEFAULT`` swallows it),
    and a negative value must not thread into ``subprocess.run(timeout=...)``."""
    for bad in (0, -5):
        with pytest.raises(ValueError, match="positive"):
            build_adapter(argparse.Namespace(swebench_timeout=bad))


# ---------------------------------------------------------------------------
# Protocol conformance + build_adapter wiring.
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol():
    """The concrete adapter structurally conforms to ``BenchmarkAdapter`` and
    ``build_adapter`` returns one (the external CLI relies on both)."""
    adapter = _adapter()
    assert isinstance(adapter, BenchmarkAdapter)
    built = build_adapter(argparse.Namespace())
    assert isinstance(built, BenchmarkAdapter)
    assert built.name


def test_default_swebench_timeout_floor():
    """A sane floor, not a brittle exact-value pin: bumped 1800 -> 3600 after a
    real Phase-1 pilot run showed 1800s was insufficient for at least one
    legitimately-slow instance (django__django-10914, killed mid-progress at the
    old cutoff). See the constant's docstring in ``swebench_lite.py`` for the
    full rationale and the note to revisit after a full screening run."""
    assert DEFAULT_SWEBENCH_TIMEOUT >= 3600


def test_build_adapter_defaults_timeout_when_swebench_timeout_omitted():
    """Non-vacuous control: a bare ``Namespace`` (the CLI flag omitted, which
    parses to ``default=None`` in ``benchmarks.runner.pilot._build_parser``)
    falls back to ``DEFAULT_SWEBENCH_TIMEOUT`` unchanged."""
    built = build_adapter(argparse.Namespace())
    assert built.timeout == DEFAULT_SWEBENCH_TIMEOUT


def test_build_adapter_honors_swebench_timeout_override():
    """``build_adapter`` reads an explicit ``swebench_timeout`` off ``args`` (as
    populated by the ``--swebench-timeout`` CLI flag) and threads it into the
    adapter's ``timeout``, overriding the default."""
    built = build_adapter(argparse.Namespace(swebench_timeout=7200))
    assert built.timeout == 7200


# ---------------------------------------------------------------------------
# Dataset loader: hermetic JSONL degrade path + lazy datasets import.
# ---------------------------------------------------------------------------


def test_dataset_loader_degrades_to_local_jsonl(tmp_path: Path):
    """With ``datasets``/HF absent, ``load_instances`` reads a local JSONL of
    instances -- proving the loader degrades and never hard-depends on HF."""
    jsonl = tmp_path / "instances.jsonl"
    recs = [
        {"instance_id": "a__1", "repo": "a/b", "base_commit": "sha1",
         "problem_statement": "p1", "test_patch": ""},
        {"instance_id": "c__2", "repo": "c/d", "base_commit": "sha2",
         "problem_statement": "p2", "test_patch": ""},
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    args = argparse.Namespace(dataset="swe-bench-lite", instances=str(jsonl))
    loaded = ds.load_instances(args)
    assert [r["instance_id"] for r in loaded] == ["a__1", "c__2"]
    assert loaded[0]["repo"] == "a/b"


def test_dataset_loader_id_filter_on_jsonl(tmp_path: Path):
    """When ``--instances`` is a comma-separated id list AND a local JSONL exists
    via ``--instances-file``, only the requested ids are returned."""
    jsonl = tmp_path / "all.jsonl"
    recs = [
        {"instance_id": "keep__1", "repo": "a/b", "base_commit": "s1",
         "problem_statement": "p", "test_patch": ""},
        {"instance_id": "drop__2", "repo": "a/b", "base_commit": "s2",
         "problem_statement": "p", "test_patch": ""},
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    args = argparse.Namespace(
        dataset="swe-bench-lite", instances="keep__1", instances_file=str(jsonl)
    )
    loaded = ds.load_instances(args)
    assert [r["instance_id"] for r in loaded] == ["keep__1"]


def test_dataset_hf_path_errors_clearly_when_datasets_absent():
    """With no local JSONL and ``datasets`` not installed, the HF path raises a
    CLEAR, actionable error (mentioning ``datasets``) -- NOT a bare ImportError at
    module import time. Proves the heavy import is lazy + degradation is explicit.
    """
    with pytest.raises(RuntimeError) as exc:
        ds.load_instances_from_hf(instance_ids=["x__1"])
    assert "datasets" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# WS9: version-aware per-instance Python for the per-instance venv (P1.6).
#
# ``_default_install_env`` used to ALWAYS build the venv from ``sys.executable``
# (AutoDev's own interpreter, 3.13 on this host) regardless of the target repo's
# era -- self-acknowledged deferred "P1.6" work. That breaks era-sensitive
# installs: ``psf/requests``'s ``setup.py`` imports ``from collections import
# Mapping`` (removed in 3.10), so a 3.13 venv fails ``pip install -e .`` before
# a solve even starts. The resolver maps a SWE-bench-Lite instance's
# ``(repo, version)`` to the era-correct CPython the OFFICIAL harness pins --
# verified against SWE-bench/SWE-bench ``swebench/harness/constants/python.py``
# @ c7a956c (see ``swebench_lite._PY_VERSION_TABLE_SOURCE``). Constant-version
# repos are a bare repo lookup; the rest use a few version-threshold buckets
# keyed off the instance's own ``version`` field. Untabulated repo, a
# below-range/unparseable version, or ``uv`` absent -> None -> today's exact
# ``sys.executable`` venv (never a hard failure, never a wrong guess).
# ---------------------------------------------------------------------------


def test_resolve_python_constant_version_repo_is_version_independent():
    """A constant-version repo (its whole SWE-bench version range pins ONE
    CPython) resolves by a bare repo lookup -- the ``version`` field is
    irrelevant. ``psf/requests`` is 3.9 across all its tabulated versions (the
    confirmed-valuable instance: its ``setup.py`` does ``from collections import
    Mapping``, gone in 3.10+, so a 3.13 venv breaks the editable install)."""
    from benchmarks.adapters import swebench_lite as adp

    assert adp._resolve_python_version("psf/requests", "2.31") == "3.9"
    # version-independent: a different (even nonsensical) version -> same answer.
    assert adp._resolve_python_version("psf/requests", "0.7") == "3.9"
    assert adp._resolve_python_version("psf/requests", None) == "3.9"
    # a second constant repo pinning a DIFFERENT constant (xarray -> 3.10).
    assert adp._resolve_python_version("pydata/xarray", "2022.03") == "3.10"


def test_resolve_python_threshold_boundary_repo():
    """A threshold repo resolves off the instance ``version`` (the highest
    bucket whose min <= version). Django is the canonical multi-bucket case:
    4.0 -> 3.8 but 4.1 -> 3.9 (a real one-minor boundary in the upstream
    specs), 2.2 -> 3.5, 3.2 -> 3.6, and 5.x -> 3.11."""
    from benchmarks.adapters import swebench_lite as adp

    assert adp._resolve_python_version("django/django", "2.2") == "3.5"
    assert adp._resolve_python_version("django/django", "3.2") == "3.6"
    # the load-bearing boundary: 4.0 and 4.1 straddle a python bump.
    assert adp._resolve_python_version("django/django", "4.0") == "3.8"
    assert adp._resolve_python_version("django/django", "4.1") == "3.9"
    assert adp._resolve_python_version("django/django", "5.0") == "3.11"


def test_resolve_python_tolerates_v_prefixed_version_astropy_boundary():
    """The version parser tolerates the upstream ``v``-prefixed key: astropy's
    only 3.10 spec is keyed ``v5.3`` while 5.0-5.2 are 3.9, so a bare and a
    v-prefixed 5.3 both resolve to 3.10 while 5.2 stays 3.9."""
    from benchmarks.adapters import swebench_lite as adp

    assert adp._resolve_python_version("astropy/astropy", "5.2") == "3.9"
    assert adp._resolve_python_version("astropy/astropy", "v5.3") == "3.10"
    assert adp._resolve_python_version("astropy/astropy", "5.3") == "3.10"


def test_resolve_python_untabulated_and_below_range_fall_back_to_none():
    """Conservative fallback: an untabulated repo, a threshold repo whose
    version is BELOW its lowest bucket, and a missing/unparseable version all
    resolve to None -> the caller keeps today's ``sys.executable`` behavior
    (never a hard failure, never a wrong guess)."""
    from benchmarks.adapters import swebench_lite as adp

    # untabulated repo (not in either table).
    assert adp._resolve_python_version("some/unknown-repo", "1.0") is None
    # threshold repo, version below the lowest bucket (flask's floor is 2.0).
    assert adp._resolve_python_version("pallets/flask", "1.0") is None
    # missing / empty / unparseable version on a threshold repo.
    assert adp._resolve_python_version("django/django", None) is None
    assert adp._resolve_python_version("django/django", "") is None
    assert adp._resolve_python_version("django/django", "not-a-version") is None


def test_venv_command_uses_uv_for_resolvable_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The venv-creation argv for a resolvable instance (repo tabulated, version
    resolves, ``uv`` available) is ``uv venv --python X.Y --seed <dir>``: uv
    provisions the era-correct interpreter on demand and ``--seed`` installs
    pip/setuptools/wheel so the downstream ``pip install -e .`` is unchanged."""
    from benchmarks.adapters import swebench_lite as adp

    monkeypatch.setattr(adp, "_uv_available", lambda: True)
    venv_dir = tmp_path / ".venv"
    instance = {
        "instance_id": "psf__requests-1963",
        "repo": "psf/requests",
        "version": "2.19",
    }
    cmd = adp._venv_creation_command(instance, venv_dir)
    assert cmd[:4] == ["uv", "venv", "--python", "3.9"]
    assert "--seed" in cmd
    assert cmd[-1] == str(venv_dir)
    # NOT the plain sys.executable fallback.
    assert sys.executable not in cmd


def test_venv_command_falls_back_to_sys_executable_when_untabulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An untabulated repo -> the plain ``sys.executable -m venv <dir>`` fallback
    (today's exact behavior) even when ``uv`` is available."""
    from benchmarks.adapters import swebench_lite as adp

    monkeypatch.setattr(adp, "_uv_available", lambda: True)  # uv present, but...
    venv_dir = tmp_path / ".venv"
    instance = {"instance_id": "x__1", "repo": "some/unknown", "version": "1.0"}
    cmd = adp._venv_creation_command(instance, venv_dir)
    assert cmd == [sys.executable, "-m", "venv", str(venv_dir)]


def test_venv_command_falls_back_when_uv_unavailable_even_if_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Even for a resolvable instance, if ``uv`` is not on PATH the argv is the
    plain ``sys.executable -m venv`` fallback -- version-awareness is a strict
    add-on that never becomes a hard dependency on uv."""
    from benchmarks.adapters import swebench_lite as adp

    monkeypatch.setattr(adp, "_uv_available", lambda: False)
    venv_dir = tmp_path / ".venv"
    instance = {
        "instance_id": "psf__requests-1963",
        "repo": "psf/requests",
        "version": "2.19",
    }
    cmd = adp._venv_creation_command(instance, venv_dir)
    assert cmd == [sys.executable, "-m", "venv", str(venv_dir)]


def test_default_install_env_invokes_uv_venv_for_resolvable_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Wiring proof: ``_default_install_env`` threads the resolved interpreter
    into the REAL venv-creation call -- the first ``_run`` argv for a resolvable
    instance is ``uv venv --python 3.9 --seed ...``, and the install step still
    shells the seeded venv's own ``pip`` (downstream unchanged)."""
    from benchmarks.adapters import swebench_lite as adp
    from benchmarks.runner.solve import _SubprocessResult

    monkeypatch.setattr(adp, "_uv_available", lambda: True)
    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)

    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout, env=None):
        calls.append(list(cmd))
        return _SubprocessResult(
            returncode=0, stdout="", stderr="", timed_out=False, elapsed_seconds=0.01
        )

    monkeypatch.setattr(adp, "_run", fake_run)

    instance = {
        "instance_id": "psf__requests-1963",
        "repo": "psf/requests",
        "version": "2.19",
    }
    result = adp._default_install_env(instance, workdir)

    assert result.installed is True
    # the venv-creation call used uv with the resolved (era-correct) interpreter.
    assert calls[0][:4] == ["uv", "venv", "--python", "3.9"]
    assert "--seed" in calls[0]
    # the install step still shells the seeded venv's pip (downstream unchanged).
    assert calls[1][1:] == ["install", "-e", "."]
    assert calls[1][0].endswith("pip")


def test_default_install_env_untabulated_still_uses_sys_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Non-vacuous control: for an untabulated instance ``_default_install_env``
    still builds the venv from ``sys.executable`` (the pre-WS9 behavior),
    proving the uv path is genuinely gated on a successful resolution."""
    from benchmarks.adapters import swebench_lite as adp
    from benchmarks.runner.solve import _SubprocessResult

    monkeypatch.setattr(adp, "_uv_available", lambda: True)
    workdir = tmp_path / "inst"
    workdir.mkdir(parents=True)

    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout, env=None):
        calls.append(list(cmd))
        return _SubprocessResult(
            returncode=0, stdout="", stderr="", timed_out=False, elapsed_seconds=0.01
        )

    monkeypatch.setattr(adp, "_run", fake_run)

    instance = {"instance_id": "x__1", "repo": "some/unknown", "version": "9.9"}
    result = adp._default_install_env(instance, workdir)

    assert result.installed is True
    assert calls[0] == [sys.executable, "-m", "venv", str(workdir / ".venv")]
