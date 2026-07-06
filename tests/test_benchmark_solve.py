"""Gate tests for the shared solve-half foundation (Phase-1 P1.1).

These pin the *reuse refactor* that extracts ``run_task``'s solve-half into
``benchmarks/runner/solve.py`` so the external SWE-bench-Lite adapter (P1.2+)
can reuse the exact autodev-driving + diff-recovery ladder.

Core gate (the three required proofs):

  (a) an ``env`` overlay on the :class:`SolveProfile` REACHES the injected
      invoker (we capture and assert the env the invoker actually received, and
      that it is *merged onto* the parent environment — not a bare echo);
  (b) a ``config_patch`` on the profile is APPLIED (deep-merged) to the freshly
      ``init``-ed ``.autodev/config.json`` — i.e. it reaches solve and mutates
      real state, preserving untouched sibling keys;
  (c) a NULL solver (an invoker that changes nothing) yields an EMPTY-diff
      :class:`SolveOutcome` that maps to FAIL/empty via the existing patch-apply
      contract — NEVER a silent PASS. Its non-vacuous control is a real solver
      whose change flows through into a non-empty outcome.

Plus: genuine-reuse proof (``run_task`` actually delegates to ``solve``), the
diff-recovery ladder precedence, and the adapter/scorer/external protocol
foundation.

Every test uses an in-memory fake invoker — no autodev subprocess, no network,
no Claude. The only subprocess is local ``git`` (to prepare a tiny baseline repo
and recover the diff), which is hermetic and offline.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.runner.run_benchmark import DEFAULT_TASKS_ROOT
from benchmarks.runner.scorer import apply_patch_to_repo
from benchmarks.runner.solve import (
    SolveOutcome,
    SolveProfile,
    solve,
)
from benchmarks.runner.task_runner import (
    _init_git_repo,
    _SubprocessResult,
    run_task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prepared_repo(root: Path, *, name: str = "wd") -> Path:
    """Materialise a tiny git repo checked out at a baseline commit.

    Reuses the harness' own ``_init_git_repo`` (git init + baseline commit), so
    ``HEAD`` is the baseline that ``solve`` recovers the diff against — exactly
    the state ``run_task`` and the SWE-bench adapter hand to ``solve``.
    """
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _init_git_repo(repo)
    return repo


def _ok(elapsed: float = 0.0) -> _SubprocessResult:
    return _SubprocessResult(
        returncode=0, stdout="", stderr="", timed_out=False, elapsed_seconds=elapsed
    )


# ---------------------------------------------------------------------------
# Gate (a): an env overlay on the profile reaches the invoker.
# ---------------------------------------------------------------------------


def test_env_overlay_reaches_invoker(tmp_path: Path):
    """RED-on-HEAD: without solve threading ``profile.env`` into the invoker's
    ``env=`` kwarg, the sentinel never arrives. GREEN: the overlay reaches the
    invoker, merged onto ``os.environ`` (so PATH survives — proving a real merge,
    not an echo of the 1-key overlay dict)."""
    repo = _prepared_repo(tmp_path)
    captured: dict = {}

    def fake_invoker(args, *, env, cwd, timeout):
        captured.setdefault("env", env)  # record the first (init) call's env
        return _ok()

    profile = SolveProfile(env={"AUTODEV_BENCH_SENTINEL": "42"}, timeout=7)
    solve(repo, "fix the bug", profile, fake_invoker)

    env = captured["env"]
    assert env is not None, "solve passed no env to the invoker"
    assert env.get("AUTODEV_BENCH_SENTINEL") == "42", "overlay value did not reach invoker"
    assert "PATH" in env, "overlay replaced rather than merged the parent env"


def test_empty_env_overlay_passes_none(tmp_path: Path):
    """Legacy parity: no overlay ⇒ the invoker is called with ``env=None`` (child
    inherits the live parent env), identical to the pre-refactor
    ``_run(..., env=None)`` path. A regression to ``env={}`` would flip this."""
    repo = _prepared_repo(tmp_path)
    seen: list = []

    def fake_invoker(args, *, env, cwd, timeout):
        seen.append(env)
        return _ok()

    solve(repo, "intent", SolveProfile(timeout=5), fake_invoker)
    assert seen, "invoker was never called"
    assert all(e is None for e in seen), f"expected env=None on every call, got {seen}"


def test_env_overlay_reaches_every_command(tmp_path: Path):
    """The overlay must reach all three autodev commands (init/plan/execute),
    not just the first — the whole solve runs under the injected env."""
    repo = _prepared_repo(tmp_path)
    envs: list = []

    def fake_invoker(args, *, env, cwd, timeout):
        envs.append((args[0] if args else "", env))
        return _ok()

    solve(repo, "intent", SolveProfile(env={"K": "V"}, timeout=5), fake_invoker)
    labels = [a for a, _ in envs]
    assert labels == ["init", "plan", "execute"], labels
    assert all(e is not None and e.get("K") == "V" for _, e in envs)


# ---------------------------------------------------------------------------
# Gate (b): a config_patch on the profile is applied after init.
# ---------------------------------------------------------------------------


def test_config_patch_deep_merged_after_init(tmp_path: Path):
    """RED-on-HEAD: without the generalised ``config_patch`` mechanism the
    freshly-``init``-ed config is untouched. GREEN: the patch is deep-merged into
    ``.autodev/config.json`` right after init — the patched keys change AND
    untouched siblings are preserved (non-vacuous: not a wholesale replace)."""
    repo = _prepared_repo(tmp_path)

    def fake_invoker(args, *, env, cwd, timeout):
        if args and args[0] == "init":
            cfg_dir = cwd / ".autodev"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(
                json.dumps(
                    {
                        "qa_gates": {"lint": True, "test_runner": True},
                        "tournaments": {"max_parallel_subprocesses": 4},
                    }
                ),
                encoding="utf-8",
            )
        return _ok()

    profile = SolveProfile(
        config_patch={
            "qa_gates": {"test_runner": False},
            "tournaments": {"max_parallel_subprocesses": 1},
        },
        timeout=5,
    )
    solve(repo, "intent", profile, fake_invoker)

    cfg = json.loads((repo / ".autodev" / "config.json").read_text(encoding="utf-8"))
    # Patch reached and applied.
    assert cfg["qa_gates"]["test_runner"] is False
    assert cfg["tournaments"]["max_parallel_subprocesses"] == 1
    # Deep-merge preserved untouched siblings.
    assert cfg["qa_gates"]["lint"] is True


def test_config_patch_noop_when_no_config(tmp_path: Path):
    """Safety parity with the old lint hook: a patch against a missing
    ``.autodev/config.json`` is a silent no-op, never a crash."""
    repo = _prepared_repo(tmp_path)

    def fake_invoker(args, *, env, cwd, timeout):
        return _ok()  # never writes a config.json

    solve(
        repo,
        "intent",
        SolveProfile(config_patch={"qa_gates": {"lint": False}}, timeout=5),
        fake_invoker,
    )
    assert not (repo / ".autodev" / "config.json").exists()


def test_empty_config_patch_leaves_config_untouched(tmp_path: Path):
    """An empty ``config_patch`` (the committed default) must not rewrite the
    config — preserving the pre-refactor behaviour where the lint hook only fired
    under an opt-in env var."""
    repo = _prepared_repo(tmp_path)
    original = json.dumps({"qa_gates": {"lint": True}}, indent=2)

    def fake_invoker(args, *, env, cwd, timeout):
        if args and args[0] == "init":
            cfg_dir = cwd / ".autodev"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(original, encoding="utf-8")
        return _ok()

    solve(repo, "intent", SolveProfile(timeout=5), fake_invoker)
    assert (repo / ".autodev" / "config.json").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Gate (c): a null solver → empty SolveOutcome → FAIL/empty (never a silent PASS).
# ---------------------------------------------------------------------------


def test_null_solver_yields_empty_outcome_mapped_to_fail(tmp_path: Path):
    """A NULL solver exits cleanly but changes nothing. The outcome must be
    flagged empty and map to a non-PASS through the EXISTING patch-apply contract
    — never a silent PASS."""
    repo = _prepared_repo(tmp_path)

    def null_invoker(args, *, env, cwd, timeout):
        return _ok()  # no file change, no ledger, no commit → no diff

    outcome = solve(repo, "intent", SolveProfile(timeout=5), null_invoker)
    assert isinstance(outcome, SolveOutcome)
    assert outcome.empty_diff is True
    assert outcome.diff == ""
    assert outcome.diff_source == "none"

    applied = apply_patch_to_repo(repo, outcome.diff)
    assert applied.applied is False
    assert applied.error == "empty diff"


def test_real_solver_yields_nonempty_outcome(tmp_path: Path):
    """Non-vacuous control for gate (c): a real solver that mutates source yields
    a NON-empty outcome whose diff carries the change. If ``empty_diff`` were a
    constant, this and the null case could not both hold."""
    repo = _prepared_repo(tmp_path)

    def real_invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            (cwd / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        return _ok()

    outcome = solve(repo, "intent", SolveProfile(timeout=5), real_invoker)
    assert outcome.empty_diff is False
    assert outcome.success is True
    assert "return 2" in outcome.diff
    assert outcome.diff_source == "commit"
    assert outcome.base_sha and outcome.base_sha != "HEAD"


def test_failed_autodev_command_marks_not_success(tmp_path: Path):
    """A non-zero autodev command sets ``success=False`` and records the reason +
    captured output, and short-circuits the remaining commands (matching
    ``run_task``'s abort-on-first-failure)."""
    repo = _prepared_repo(tmp_path)
    calls: list = []

    def failing_invoker(args, *, env, cwd, timeout):
        calls.append(args[0] if args else "")
        if args and args[0] == "plan":
            return _SubprocessResult(
                returncode=2,
                stdout="usage...",
                stderr="Error: No such option: --spec",
                timed_out=False,
                elapsed_seconds=0.01,
            )
        return _ok()

    outcome = solve(repo, "intent", SolveProfile(timeout=5), failing_invoker)
    assert outcome.success is False
    # The reason embeds the full step label (exactly as run_task always did), so
    # it identifies the failing command (plan) and the exit code.
    assert outcome.failed_reason is not None
    assert outcome.failed_reason.startswith("autodev plan")
    assert outcome.failed_reason.endswith("exited 2")
    assert "No such option" in outcome.fail_stderr_tail
    # Aborted on plan — execute never ran.
    assert calls == ["init", "plan"]
    assert outcome.invocations == 2


# ---------------------------------------------------------------------------
# Diff-recovery ladder precedence (ledger → commit → worktree).
# ---------------------------------------------------------------------------


def test_ledger_diff_takes_precedence_over_worktree(tmp_path: Path):
    """The ladder must consult the ledger first (``ledger or commit or head``):
    a ledger diff wins even when the worktree also changed. Preserves
    ``run_task``'s exact short-circuit order."""
    repo = _prepared_repo(tmp_path)
    ledger_diff = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n"
        "-def f():\n-    return 1\n+def f():\n+    return 9\n"
    )

    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            (cwd / "mod.py").write_text("def f():\n    return 5\n", encoding="utf-8")
            led = cwd / ".autodev"
            led.mkdir(parents=True, exist_ok=True)
            (led / "plan-ledger.jsonl").write_text(
                json.dumps({"event": "execute_diff", "diff": ledger_diff}) + "\n",
                encoding="utf-8",
            )
        return _ok()

    outcome = solve(repo, "intent", SolveProfile(timeout=5), invoker)
    assert outcome.diff == ledger_diff
    assert outcome.diff_source == "ledger"


# ---------------------------------------------------------------------------
# Genuine reuse: run_task delegates its solve-half to solve() (not dead code).
# ---------------------------------------------------------------------------


def test_run_task_delegates_to_solve(tmp_path: Path, monkeypatch):
    """run_task must actually call solve() for its solve-half. Spying the name
    proves genuine reuse (if run_task re-implemented the loop instead, the spy
    would never fire) AND that behaviour is preserved (ledgered fix → PASS)."""
    import benchmarks.runner.task_runner as tr

    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"
    gt_patch = (task_dir / "ground_truth.patch").read_text(encoding="utf-8")
    spec_text = (task_dir / "spec.md").read_text(encoding="utf-8")
    seen: dict = {}
    real_solve = tr.solve

    def spy(workdir, intent, profile, invoker):
        seen["called"] = True
        seen["intent"] = intent
        seen["profile"] = profile
        return real_solve(workdir, intent, profile, invoker)

    monkeypatch.setattr(tr, "solve", spy)

    def stub(args, cwd, timeout):
        if "execute" in args:
            led = cwd / ".autodev"
            led.mkdir(parents=True, exist_ok=True)
            (led / "plan-ledger.jsonl").write_text(
                json.dumps({"event": "execute_diff", "diff": gt_patch}) + "\n",
                encoding="utf-8",
            )
        return _ok(0.01)

    result = run_task(task_dir, autodev_invoker=stub, workdir_root=tmp_path)
    assert seen.get("called") is True, "run_task did NOT delegate to solve() (dead reuse)"
    assert seen["intent"] == spec_text
    assert isinstance(seen["profile"], SolveProfile)
    assert result.status == "PASS", result.error or result.stderr_tail


# ---------------------------------------------------------------------------
# Adapter / Scorer / external-CLI protocol foundation.
# ---------------------------------------------------------------------------


def test_adapter_protocol_drives_solve_end_to_end(tmp_path: Path):
    """A minimal adapter that satisfies the ``BenchmarkAdapter`` protocol composes
    with ``solve``: prepare → intent → solve → predict yields a prediction record
    carrying the solved change. Proves the protocol is real and usable."""
    from benchmarks.adapters.base import BenchmarkAdapter

    class FakeAdapter:
        name = "fake"

        def prepare(self, instance, workdir):
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            _init_git_repo(workdir)
            return SolveProfile(timeout=5)

        def intent(self, instance):
            return instance["problem_statement"]

        def predict(self, instance, workdir, outcome):
            return {
                "instance_id": instance["instance_id"],
                "model_name_or_path": self.name,
                "model_patch": outcome.diff,
            }

    adapter = FakeAdapter()
    assert isinstance(adapter, BenchmarkAdapter)  # structural conformance

    instance = {"instance_id": "demo__1", "problem_statement": "make f return 2"}
    workdir = tmp_path / "inst"
    profile = adapter.prepare(instance, workdir)
    intent = adapter.intent(instance)

    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            (cwd / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        return _ok()

    outcome = solve(workdir, intent, profile, invoker)
    pred = adapter.predict(instance, workdir, outcome)
    assert pred["instance_id"] == "demo__1"
    assert pred["model_name_or_path"] == "fake"
    assert "return 2" in pred["model_patch"]


def test_score_report_counts_pass_fail_error():
    """``ScoreReport.counts`` tallies PASS/FAIL/ERROR distinctly — ERROR is
    first-class and never folded into FAIL (anti-vacuity for the coarse gate)."""
    from benchmarks.scorers.base import ERROR, FAIL, InstanceScore, PASS, ScoreReport

    report = ScoreReport(
        instances=[
            InstanceScore("a", PASS),
            InstanceScore("b", FAIL),
            InstanceScore("c", ERROR),
            InstanceScore("d", PASS),
        ]
    )
    assert report.counts() == {"passed": 2, "failed": 1, "errored": 1, "total": 4}


def test_scorer_protocol_is_implementable():
    """A fake scorer that implements ``score`` satisfies the ``Scorer`` protocol
    and returns a ``ScoreReport`` — proving the protocol is usable."""
    from benchmarks.scorers.base import InstanceScore, PASS, ScoreReport, Scorer

    class FakeScorer:
        name = "fake"

        def score(self, predictions, *, run_id):
            return ScoreReport(
                instances=[InstanceScore(p["instance_id"], PASS) for p in predictions]
            )

    scorer = FakeScorer()
    assert isinstance(scorer, Scorer)
    report = scorer.score([{"instance_id": "x"}], run_id="r1")
    assert report.counts() == {"passed": 1, "failed": 0, "errored": 0, "total": 1}


def test_external_cli_solve_then_score_roundtrip(tmp_path: Path):
    """The external ``--solve-only`` / ``--score-only`` core functions compose
    over the adapter/scorer protocols: run_solve writes ``predictions.jsonl``,
    run_score reads it back and scores. Thin but functional — no autodev, no
    network, injected fakes only."""
    from benchmarks.runner.external import run_score, run_solve
    from benchmarks.scorers.base import InstanceScore, PASS, ScoreReport

    class FakeAdapter:
        name = "fake"

        def prepare(self, instance, workdir):
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            _init_git_repo(workdir)
            return SolveProfile(timeout=5)

        def intent(self, instance):
            return instance["problem_statement"]

        def predict(self, instance, workdir, outcome):
            return {
                "instance_id": instance["instance_id"],
                "model_name_or_path": self.name,
                "model_patch": outcome.diff,
            }

    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            (cwd / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        return _ok()

    class FakeScorer:
        name = "fake"

        def score(self, predictions, *, run_id):
            return ScoreReport(
                instances=[
                    InstanceScore(
                        p["instance_id"],
                        PASS if p.get("model_patch") else "FAIL",
                    )
                    for p in predictions
                ]
            )

    instances = [{"instance_id": "demo__1", "problem_statement": "make f return 2"}]
    preds_path = tmp_path / "predictions.jsonl"
    predictions = run_solve(
        FakeAdapter(),
        instances,
        invoker,
        workdir_root=tmp_path / "work",
        predictions_out=preds_path,
    )
    assert preds_path.is_file()
    assert predictions[0]["instance_id"] == "demo__1"
    assert "return 2" in predictions[0]["model_patch"]

    report_path = tmp_path / "report.json"
    report = run_score(
        FakeScorer(), preds_path, run_id="r1", report_out=report_path
    )
    assert report.counts() == {"passed": 1, "failed": 0, "errored": 0, "total": 1}
    assert report_path.is_file()
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["run_id"] == "r1"
    assert saved["summary"]["passed"] == 1


def test_run_solve_isolates_bad_instance_prepare_and_continues(tmp_path: Path):
    """``external.run_solve`` mirrors the guard's isolation: an adapter ``prepare``
    that raises ``InstancePrepareError`` for one instance is recorded as a
    patch-less prediction (scorer → ERROR) and the sweep CONTINUES over the rest —
    a bad ``base_commit`` never aborts the whole solve run.

    Non-vacuous: before the isolation the exception propagated out of ``run_solve``,
    so this call raised instead of returning predictions for all three."""
    from benchmarks.adapters.base import InstancePrepareError
    from benchmarks.runner.external import run_solve

    class FakeAdapter:
        name = "fake"
        model_name = "fake"

        def prepare(self, instance, workdir):
            if instance["instance_id"] == "bad-2":
                raise InstancePrepareError("invalid base_commit")
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            _init_git_repo(workdir)
            return SolveProfile(timeout=5)

        def intent(self, instance):
            return instance["problem_statement"]

        def predict(self, instance, workdir, outcome):
            return {
                "instance_id": instance["instance_id"],
                "model_name_or_path": self.name,
                "model_patch": outcome.diff,
            }

    def invoker(args, *, env, cwd, timeout):
        if args and args[0] == "execute":
            (cwd / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        return _ok()

    instances = [
        {"instance_id": "ok-1", "problem_statement": "make f return 2"},
        {"instance_id": "bad-2", "problem_statement": "make f return 2"},
        {"instance_id": "ok-3", "problem_statement": "make f return 2"},
    ]
    preds_path = tmp_path / "predictions.jsonl"
    predictions = run_solve(
        FakeAdapter(),
        instances,
        invoker,
        workdir_root=tmp_path / "work",
        predictions_out=preds_path,
    )

    # all three instances accounted for — the bad one did NOT abort the sweep
    assert len(predictions) == 3
    by_id = {p["instance_id"]: p for p in predictions}
    assert "return 2" in by_id["ok-1"]["model_patch"]
    assert "return 2" in by_id["ok-3"]["model_patch"]
    # the bad instance carries a patch-less record (scorer marks it ERROR)
    assert by_id["bad-2"]["model_patch"] == ""
    assert by_id["bad-2"]["model_name_or_path"] == "fake"


def test_external_cli_parser_requires_a_subflow():
    """The CLI wires a real argparse with mutually-exclusive
    ``--solve-only``/``--score-only``; neither ⇒ a non-zero error, not a crash."""
    from benchmarks.runner.external import main

    rc = main([])
    assert rc != 0
