"""Gate tests for the sb-cli cloud scorer (Phase-1 P1.3).

These pin the three required proofs (a)-(c) from the plan
(``thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md``):

  (a) the ``predictions.jsonl`` handed to ``sb-cli submit`` has EXACTLY one JSON
      object per prediction with the SWE-bench triple keys
      ``{instance_id, model_name_or_path, model_patch}`` and the right values;
  (b) the generated report's ``resolved_ids`` drive a correct per-instance
      PASS (resolved) / FAIL (unresolved) split;
  (c) a submission failure (non-zero exit / raised exception / missing report /
      absent API key) yields ERROR for every instance -- NEVER a FAIL that would
      read as a capability regression.

Every test is hermetic + offline: ``sb-cli`` is NEVER imported as a module and is
NEVER really invoked -- the subprocess layer is replaced by an in-memory fake
runner, and the report is a tiny in-repo fixture dict. No network, no CLI, no
Claude subscription is touched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from benchmarks.runner.solve import _SubprocessResult
from benchmarks.scorers.base import ERROR, FAIL, PASS, Scorer, ScoreReport
from benchmarks.scorers.sbcli import (
    API_KEY_ENV,
    SbcliScorer,
    build_scorer,
)

# ---------------------------------------------------------------------------
# Fixtures: predictions + a SWE-bench-style report.
# ---------------------------------------------------------------------------

_PATCH_A = (
    "diff --git a/mod.py b/mod.py\n"
    "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-    return 1\n+    return 2\n"
)
_PATCH_B = (
    "diff --git a/other.py b/other.py\n"
    "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-    x = 1\n+    x = 2\n"
)


def _predictions() -> list[dict[str, Any]]:
    return [
        {"instance_id": "demo__repo-1", "model_name_or_path": "autodev",
         "model_patch": _PATCH_A},
        {"instance_id": "demo__repo-2", "model_name_or_path": "autodev",
         "model_patch": _PATCH_B},
    ]


def _report(resolved: Sequence[str], unresolved: Sequence[str],
            errored: Sequence[str] = (), failed: Sequence[str] = ()) -> dict[str, Any]:
    """A SWE-bench sb-cli report shaped like the real thing (superset is fine).

    ``failed`` models the REAL sb-cli ``failed_ids`` field (see the slice4
    forensic re-grade fixtures under ``~/bench-forensics/slice4/sbcli-rescore/``
    and ``sbcli-goldctl/``, outside this repo): instances the cloud eval itself
    never completed for -- deliberately excluded from ``completed_instances``,
    exactly like the real report.
    """
    return {
        "total_instances": len(resolved) + len(unresolved) + len(errored) + len(failed),
        "submitted_instances": len(resolved) + len(unresolved) + len(errored) + len(failed),
        "completed_instances": len(resolved) + len(unresolved),
        "resolved_instances": len(resolved),
        "unresolved_instances": len(unresolved),
        "error_instances": len(errored),
        "failed_instances": len(failed),
        "resolved_ids": list(resolved),
        "unresolved_ids": list(unresolved),
        "error_ids": list(errored),
        "failed_ids": list(failed),
    }


def _real_slice4_report_shape(submitted_ids: Sequence[str]) -> dict[str, Any]:
    """The ACTUAL sb-cli report shape captured by the slice4 forensic re-grade
    (``~/bench-forensics/slice4/sbcli-rescore/swe-bench_lite__test__slice4-rescore.json``
    and ``.../sbcli-goldctl/swe-bench_lite__test__slice4-goldctl.json``, both
    outside this repo): submitting canonical GOLD patches for all 10
    SWE-bench-Lite instances scored 10/10 "FAIL" with ``completed_instances=0``
    -- EVERY submitted instance lands in ``failed_ids`` and the cloud eval never
    ran the hidden tests at all. This reproduces every field name/shape the real
    report carries (``schema_version``, ``completed_ids``, ``pending_ids``, ...);
    the 291-row ``incomplete_ids`` (unsubmitted dataset rows, irrelevant noise
    for this unit) is omitted -- the scorer never reads it.
    """
    ids = list(submitted_ids)
    return {
        "total_instances": 300,
        "submitted_instances": len(ids),
        "completed_instances": 0,
        "pending_instances": 0,
        "failed_instances": len(ids),
        "resolved_instances": 0,
        "unresolved_instances": 0,
        "error_instances": 0,
        "completed_ids": [],
        "submitted_ids": list(ids),
        "error_ids": [],
        "schema_version": 2,
        "resolved_ids": [],
        "unresolved_ids": [],
        "pending_ids": [],
        "failed_ids": list(ids),
    }


def _flag(cmd: Sequence[str], name: str) -> str | None:
    """Extract the value following ``name`` in a subprocess command list."""
    tokens = list(cmd)
    for i, tok in enumerate(tokens):
        if tok == name and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _make_runner(
    *,
    report: dict[str, Any] | None = None,
    returncode: int = 0,
    raise_exc: BaseException | None = None,
    capture: dict[str, Any] | None = None,
):
    """Build a fake subprocess runner standing in for ``sb-cli submit``.

    It parses the command exactly as the real CLI would (``--predictions_path``,
    ``--run_id``, ``--output_dir``), optionally records what it saw, and -- on a
    zero-exit run -- writes the fixture ``report`` where the scorer will look for
    it (``<output_dir>/<run_id>.json``). Non-zero / raising simulates a submit
    failure. Never touches the network."""

    def runner(cmd: Sequence[str], *, cwd: Path, timeout: int,
               env: dict[str, str] | None = None) -> _SubprocessResult:
        if capture is not None:
            capture["cmd"] = list(cmd)
            preds_path = _flag(cmd, "--predictions_path")
            if preds_path:
                capture["predictions_raw"] = Path(preds_path).read_text(
                    encoding="utf-8"
                )
        if raise_exc is not None:
            raise raise_exc
        if returncode == 0 and report is not None:
            out_dir = _flag(cmd, "--output_dir")
            run_id = _flag(cmd, "--run_id")
            assert out_dir and run_id, "scorer must pass --output_dir and --run_id"
            (Path(out_dir) / f"{run_id}.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
        return _SubprocessResult(
            returncode=returncode,
            stdout="",
            stderr="" if returncode == 0 else "sb-cli: submission failed\n",
            timed_out=False,
            elapsed_seconds=0.0,
        )

    return runner


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch):
    """Give the scorer a (dummy) API key by default so most tests exercise the
    happy path; the missing-key test overrides this by deleting it."""
    monkeypatch.setenv(API_KEY_ENV, "dummy-token")


# ---------------------------------------------------------------------------
# Gate (a): predictions.jsonl schema/content.
# ---------------------------------------------------------------------------


def test_predictions_jsonl_schema_and_content(tmp_path: Path):
    """The scorer writes one JSON object per prediction, each with EXACTLY the
    SWE-bench triple keys and the values from the input records."""
    capture: dict[str, Any] = {}
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(resolved=["demo__repo-1", "demo__repo-2"], unresolved=[]),
            capture=capture,
        ),
        workdir=tmp_path,
    )
    scorer.score(_predictions(), run_id="run-a")

    raw = capture["predictions_raw"]
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 2, "one JSONL line per prediction"
    recs = [json.loads(ln) for ln in lines]
    for rec, src in zip(recs, _predictions()):
        assert set(rec.keys()) == {
            "instance_id", "model_name_or_path", "model_patch"
        }
        assert rec["instance_id"] == src["instance_id"]
        assert rec["model_name_or_path"] == src["model_name_or_path"]
        assert rec["model_patch"] == src["model_patch"]


def test_submit_command_shape(tmp_path: Path):
    """The submit command is ``sb-cli submit swe-bench_lite test`` with the
    predictions/run-id flags -- proving we invoke the CLI, never import it."""
    capture: dict[str, Any] = {}
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(resolved=["demo__repo-1"], unresolved=["demo__repo-2"]),
            capture=capture,
        ),
        workdir=tmp_path,
    )
    scorer.score(_predictions(), run_id="run-cmd")

    cmd = capture["cmd"]
    assert cmd[0] == "sb-cli"
    assert cmd[1] == "submit"
    assert "swe-bench_lite" in cmd
    assert "test" in cmd
    assert _flag(cmd, "--run_id") == "run-cmd"
    assert _flag(cmd, "--gen_report") == "1"
    assert _flag(cmd, "--predictions_path")


# ---------------------------------------------------------------------------
# Gate (b): resolved_ids -> PASS/FAIL split.
# ---------------------------------------------------------------------------


def test_resolved_ids_drive_pass_fail_split(tmp_path: Path):
    """Instance-1 is in ``resolved_ids`` (PASS); instance-2 is unresolved (FAIL).
    The verdicts must follow the report -- and no instance may be ERROR here."""
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(resolved=["demo__repo-1"], unresolved=["demo__repo-2"]),
        ),
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-b")

    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id == {"demo__repo-1": PASS, "demo__repo-2": FAIL}
    counts = report.counts()
    assert counts["passed"] == 1
    assert counts["failed"] == 1
    assert counts["errored"] == 0


def test_resolved_split_is_load_bearing(tmp_path: Path):
    """Non-vacuous control for gate (b): FLIP which id is resolved and the PASS/FAIL
    verdicts flip with it -- proving the split is read from the report, not fixed."""
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(resolved=["demo__repo-2"], unresolved=["demo__repo-1"]),
        ),
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-b2")
    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id == {"demo__repo-1": FAIL, "demo__repo-2": PASS}


def test_report_error_ids_are_error_not_fail(tmp_path: Path):
    """An instance the cloud eval itself errored on (``error_ids``) is ERROR, never
    FAIL -- infra flakiness on the scoring side is not a capability verdict."""
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(
                resolved=["demo__repo-1"], unresolved=[], errored=["demo__repo-2"]
            ),
        ),
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-err")
    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id["demo__repo-1"] == PASS
    assert by_id["demo__repo-2"] == ERROR


# ---------------------------------------------------------------------------
# WS-1 forensic fix: a submitted-but-not-completed sb-cli eval is ERROR,
# never FAIL. Proven by the gold control (X5 re-grade): submitting canonical
# gold patches for all 10 SWE-bench-Lite instances scored 10/10 "FAIL" with
# completed=0 -- the cloud eval fast-fails before running the hidden tests, and
# pre-fix code mislabelled that infra non-completion as a capability FAIL,
# making a benchmark PASS structurally impossible regardless of patch quality.
# ---------------------------------------------------------------------------


def test_slice4_forensic_shape_all_incomplete_is_error_never_fail(tmp_path: Path):
    """RED-before-fix regression, pinned to the ACTUAL sb-cli report shape from
    the slice4 forensic re-grade: every submitted instance lands in
    ``failed_ids`` with ``completed_instances=0`` -- pre-fix code mapped these to
    FAIL,"unresolved" (the "else" catch-all); they MUST become ERROR, honoring
    the file's own doctrine (module docstring: "ERROR is never folded into
    FAIL")."""
    ids = ["demo__repo-1", "demo__repo-2"]
    preds = [
        {"instance_id": i, "model_name_or_path": "autodev", "model_patch": _PATCH_A}
        for i in ids
    ]
    scorer = SbcliScorer(
        runner=_make_runner(report=_real_slice4_report_shape(ids)),
        workdir=tmp_path,
    )
    report = scorer.score(preds, run_id="run-infra")

    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id == {i: ERROR for i in ids}, (
        "a submitted-but-not-completed sb-cli eval must be ERROR, never FAIL"
    )
    assert all(s.status != FAIL for s in report.instances)
    for s in report.instances:
        assert s.detail and "did not complete" in s.detail.lower()
    # the forensic-instrumentation ask: completed/submitted surfaced on summary.
    assert report.summary["completed"] == 0
    assert report.summary["submitted"] == len(ids)


def test_normal_report_mixed_split_with_per_instance_infra_flag_inside_completed_batch(
    tmp_path: Path,
):
    """Regression/non-vacuous control: a NORMAL, genuinely-completed report
    (completed > 0) still yields PASS for resolved and FAIL for genuinely-
    completed-unresolved -- AND an id sb-cli itself flags in ``failed_ids``
    stays ERROR even though the BATCH overall completed, proving the
    per-instance ``failed_ids`` check is load-bearing on its own, not just the
    batch-wide ``completed == 0`` short-circuit."""
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(
                resolved=["r-pass"],
                unresolved=["r-fail"],
                errored=["r-error"],
                failed=["r-infra"],
            ),
        ),
        workdir=tmp_path,
    )
    preds = [
        {"instance_id": i, "model_name_or_path": "autodev", "model_patch": _PATCH_A}
        for i in ("r-pass", "r-fail", "r-error", "r-infra")
    ]
    report = scorer.score(preds, run_id="run-mixed")

    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id == {
        "r-pass": PASS,
        "r-fail": FAIL,
        "r-error": ERROR,
        "r-infra": ERROR,
    }
    infra_detail = next(
        s.detail for s in report.instances if s.instance_id == "r-infra"
    )
    assert infra_detail and "did not complete" in infra_detail.lower()
    error_detail = next(
        s.detail for s in report.instances if s.instance_id == "r-error"
    )
    assert error_detail == "sb-cli reported an evaluation error"


def test_instance_absent_from_every_report_bucket_is_error_not_fail(tmp_path: Path):
    """An id submitted but absent from resolved/unresolved/error/failed entirely
    (e.g. still genuinely "pending") must be ERROR -- the pre-fix "else -> FAIL"
    catch-all would have wrongly scored it FAIL."""
    scorer = SbcliScorer(
        runner=_make_runner(report=_report(resolved=["ok"], unresolved=[])),
        workdir=tmp_path,
    )
    preds = [
        {"instance_id": i, "model_name_or_path": "autodev", "model_patch": _PATCH_A}
        for i in ("ok", "still-pending")
    ]
    report = scorer.score(preds, run_id="run-pending")
    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id["ok"] == PASS
    assert by_id["still-pending"] == ERROR


# ---------------------------------------------------------------------------
# Gate (c): submit failure -> ERROR, never FAIL.
# ---------------------------------------------------------------------------


def test_nonzero_submit_exit_is_error_never_fail(tmp_path: Path):
    """A non-zero ``sb-cli`` exit -> EVERY instance ERROR (with the reason), and
    NOT ONE marked FAIL/PASS. A rate-limited/failed submit must never look like a
    capability regression."""
    scorer = SbcliScorer(
        runner=_make_runner(returncode=2),
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-c1")

    statuses = {s.status for s in report.instances}
    assert statuses == {ERROR}, "submit failure must be all-ERROR"
    assert all(s.status != FAIL for s in report.instances)
    assert report.counts()["errored"] == 2
    # The reason is recorded (non-empty) -- an ERROR that says why.
    assert all(s.detail for s in report.instances)


def test_raised_subprocess_error_is_error_never_fail(tmp_path: Path):
    """If the runner RAISES (e.g. ``sb-cli`` not installed / OSError), that is an
    ERROR for all instances, never a FAIL and never an uncaught crash."""
    scorer = SbcliScorer(
        runner=_make_runner(raise_exc=FileNotFoundError("sb-cli not found")),
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-c2")
    assert {s.status for s in report.instances} == {ERROR}
    assert all("sb-cli" in (s.detail or "") for s in report.instances)


def test_missing_report_after_success_is_error(tmp_path: Path):
    """A zero-exit submit that produces NO parseable report (network dropped after
    accept, CLI wrote nothing) -> ERROR, not a silent all-FAIL."""
    scorer = SbcliScorer(
        runner=_make_runner(report=None, returncode=0),  # rc 0 but writes no report
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-c3")
    assert {s.status for s in report.instances} == {ERROR}


def test_missing_api_key_is_error_no_submission(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path):
    """With no ``SWEBENCH_API_KEY``, the scorer does NOT invoke sb-cli at all and
    marks every instance ERROR (a config problem is never a FAIL)."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    called = {"n": 0}

    def runner(cmd, *, cwd, timeout, env=None):  # pragma: no cover - must not run
        called["n"] += 1
        raise AssertionError("sb-cli must not be invoked without an API key")

    scorer = SbcliScorer(runner=runner, workdir=tmp_path)
    report = scorer.score(_predictions(), run_id="run-c4")
    assert called["n"] == 0
    assert {s.status for s in report.instances} == {ERROR}
    assert all(API_KEY_ENV in (s.detail or "") for s in report.instances)


def test_empty_patch_prediction_is_error_and_not_submitted(tmp_path: Path):
    """A prediction with an empty ``model_patch`` is a no-op, not a capability FAIL:
    it is marked ERROR and EXCLUDED from the submitted predictions.jsonl (so the
    scorer never asks the cloud to grade an empty attempt)."""
    capture: dict[str, Any] = {}
    preds = _predictions()
    preds[1]["model_patch"] = "   \n"  # instance-2 has no real patch
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(resolved=["demo__repo-1"], unresolved=[]),
            capture=capture,
        ),
        workdir=tmp_path,
    )
    report = scorer.score(preds, run_id="run-empty")

    submitted = [json.loads(ln) for ln in capture["predictions_raw"].splitlines()
                 if ln.strip()]
    assert [r["instance_id"] for r in submitted] == ["demo__repo-1"]
    by_id = {s.instance_id: s.status for s in report.instances}
    assert by_id["demo__repo-1"] == PASS
    assert by_id["demo__repo-2"] == ERROR


def test_all_empty_patches_skip_submission_entirely(tmp_path: Path):
    """If NO prediction carries a real patch, the scorer never invokes sb-cli and
    every instance is ERROR (hermetic: no network even attempted)."""
    called = {"n": 0}

    def runner(cmd, *, cwd, timeout, env=None):  # pragma: no cover - must not run
        called["n"] += 1
        raise AssertionError("must not submit when there is nothing to score")

    preds = [
        {"instance_id": "a__1", "model_name_or_path": "autodev", "model_patch": ""},
        {"instance_id": "b__2", "model_name_or_path": "autodev", "model_patch": "  "},
    ]
    scorer = SbcliScorer(runner=runner, workdir=tmp_path)
    report = scorer.score(preds, run_id="run-none")
    assert called["n"] == 0
    assert {s.status for s in report.instances} == {ERROR}


# ---------------------------------------------------------------------------
# Protocol conformance + build_scorer wiring.
# ---------------------------------------------------------------------------


def test_scorer_satisfies_protocol_and_builds():
    """The concrete scorer structurally conforms to ``Scorer`` and ``build_scorer``
    returns one (the external CLI's ``_load_scorer`` relies on both)."""
    scorer = SbcliScorer()
    assert isinstance(scorer, Scorer)
    built = build_scorer(argparse.Namespace())
    assert isinstance(built, Scorer)
    assert built.name


def test_build_scorer_wires_workdir_from_out_dir():
    """WS-1 forensic-instrumentation fix: build_scorer must read ``out_dir`` from
    ``args`` and pass ``workdir=out_dir/"sbcli"`` so ``predictions.jsonl`` + the
    raw ``<run_id>.json`` report PERSIST for forensic inspection instead of a
    temp dir that gets deleted (the retention path already exists in
    ``score()``'s ``own_tmp`` branch when ``workdir`` is set)."""
    args = argparse.Namespace(out_dir=Path("/tmp/some-pilot-run"))
    built = build_scorer(args)
    assert built._workdir == Path("/tmp/some-pilot-run") / "sbcli"


def test_build_scorer_persists_predictions_and_report_under_out_dir_sbcli(
    tmp_path: Path,
):
    """End-to-end (black-box) confirmation of the retention path: scoring
    through a ``build_scorer``-built scorer actually leaves ``predictions.jsonl``
    and the raw report on disk under ``out_dir/"sbcli"`` -- the whole point of
    the forensic-instrumentation fix."""
    out_dir = tmp_path / "pilot-out"
    built = build_scorer(argparse.Namespace(out_dir=out_dir))
    built._runner = _make_runner(
        report=_report(resolved=["demo__repo-1"], unresolved=[])
    )
    report = built.score(
        [
            {
                "instance_id": "demo__repo-1",
                "model_name_or_path": "autodev",
                "model_patch": _PATCH_A,
            }
        ],
        run_id="run-persist",
    )
    assert report.instances[0].status == PASS
    assert (out_dir / "sbcli" / "predictions.jsonl").is_file()
    assert (out_dir / "sbcli" / "run-persist.json").is_file()


def test_build_scorer_workdir_none_when_out_dir_absent():
    """Back-compat: a bare ``argparse.Namespace()`` (no ``out_dir`` attribute,
    e.g. an older/unrelated caller of ``build_scorer``) must not crash and
    defaults ``workdir`` to ``None`` (own temp dir, cleaned up) -- unchanged
    pre-fix behaviour."""
    built = build_scorer(argparse.Namespace())
    assert built._workdir is None


def test_score_returns_scorereport(tmp_path: Path):
    """``score`` returns a ``ScoreReport`` whose instance count matches the inputs
    (one verdict per prediction, always)."""
    scorer = SbcliScorer(
        runner=_make_runner(
            report=_report(resolved=["demo__repo-1"], unresolved=["demo__repo-2"]),
        ),
        workdir=tmp_path,
    )
    report = scorer.score(_predictions(), run_id="run-shape")
    assert isinstance(report, ScoreReport)
    assert len(report.instances) == 2
