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
from pathlib import Path

import pytest

from benchmarks.adapters.base import BenchmarkAdapter
from benchmarks.adapters.swebench_lite import (
    CANDIDATE,
    SwebenchLiteAdapter,
    build_adapter,
)
from benchmarks.datasets import swebench_lite as ds
from benchmarks.runner.solve import (
    _git,
    _rev_parse_head,
    solve,
)
from benchmarks.scorers.base import ERROR

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


def _adapter(**kwargs) -> SwebenchLiteAdapter:
    """Adapter wired with the fake cloner + a install-OK stub, unless overridden."""
    kwargs.setdefault("cloner", _fake_cloner)
    kwargs.setdefault("env_installer", lambda instance, workdir: True)
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
    ``max_parallel_subprocesses=1``. End-to-end, the patch flips real config
    state (deep-merged, siblings preserved)."""
    adapter = _adapter(env_installer=lambda instance, workdir: False)  # install FAILS
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)

    # (1) the effective config_patch on the profile is degraded blind.
    assert profile.config_patch["qa_gates"]["test_runner"] is False
    assert profile.config_patch["tournaments"]["max_parallel_subprocesses"] == 1

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

    # (3) the blind degradation is recorded on the report.
    assert adapter.reports[-1].degraded_blind is True


def test_venv_install_success_keeps_test_runner_on(tmp_path: Path):
    """Non-vacuous control for gate (d): when the install SUCCEEDS, the config_patch
    does NOT force ``test_runner`` off (self-repair stays engaged) and the report is
    not blind -- but the burst cut still applies."""
    adapter = _adapter(env_installer=lambda instance, workdir: True)  # install OK
    instance = _make_instance()
    workdir = tmp_path / "inst"

    profile = adapter.prepare(instance, workdir)

    assert "qa_gates" not in profile.config_patch, (
        "install-OK must not force test_runner off"
    )
    assert profile.config_patch["tournaments"]["max_parallel_subprocesses"] == 1

    outcome = solve(workdir, adapter.intent(instance), profile,
                    _writing_invoker({"mod.py": FIXED_SRC}))
    adapter.predict(instance, workdir, outcome)
    assert adapter.reports[-1].degraded_blind is False


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
