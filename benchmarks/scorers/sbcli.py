"""sb-cli cloud scorer for SWE-bench-Lite (Phase-1 P1.3).

This is the score-half counterpart to the host-arm64 solve adapter
(:mod:`benchmarks.adapters.swebench_lite`). It takes the prediction records the
adapter produced, submits them to the free SWE-bench evaluation service via the
``sb-cli`` command-line tool, and turns the generated report's ``resolved_ids``
into a per-instance PASS/FAIL/ERROR verdict.

Design invariants (per the Phase-1 plan and ``benchmarks/CONTEXT.md``):

- **sb-cli is invoked as a SUBPROCESS, never imported as a module.** The
  subprocess call is behind an injectable ``runner`` so the whole scorer is
  unit-testable with no network, no CLI, and no real submission.
- **ERROR is first-class and never folded into FAIL.** A submission failure
  (non-zero exit, a raised ``OSError``/``FileNotFoundError`` when ``sb-cli`` is
  absent, an unparseable/missing report, or a missing ``SWEBENCH_API_KEY``) marks
  every affected instance ERROR with the reason recorded — it can never
  masquerade as a capability regression. Only a report that actually came back
  can turn an instance into PASS/FAIL.
- **An empty ``model_patch`` is a no-op, not a FAIL.** Such predictions are marked
  ERROR and excluded from the submitted ``predictions.jsonl`` (the cloud is never
  asked to grade an empty attempt). If nothing has a real patch, sb-cli is not
  invoked at all.
- **A submitted-but-not-completed eval is ERROR, never FAIL.** sb-cli can accept
  a submission (exit 0, a report is written) yet the cloud evaluation never
  actually runs the hidden tests for some or all instances — the slice4 forensic
  re-grade proved this: submitting canonical GOLD patches for all 10
  SWE-bench-Lite instances scored 10/10 "FAIL" with ``completed_instances=0``,
  making a PASS structurally impossible regardless of patch quality. An instance
  only ever becomes FAIL when the report shows it genuinely completed; every
  other outcome (flagged in ``failed_ids``, or simply absent from a completed
  result) is ERROR with an infra detail, never a silent FAIL.

Verdict mapping from a report: an instance in ``resolved_ids`` → PASS; in
``error_ids`` → ERROR; genuinely completed (``completed_instances`` > 0) AND in
``unresolved_ids`` AND NOT in ``failed_ids`` → FAIL ("unresolved" — the ONLY FAIL
path); anything else — in ``failed_ids``, or simply absent from every id set
(the eval did not complete for that instance) — → ERROR.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks.runner.solve import _SubprocessResult, _run
from benchmarks.scorers.base import (
    ERROR,
    FAIL,
    PASS,
    InstanceScore,
    ScoreReport,
)

# Environment variable holding the free SWE-bench submission token (NOT an
# Anthropic key — see benchmarks/CONTEXT.md).
API_KEY_ENV = "SWEBENCH_API_KEY"

# sb-cli invocation defaults.
DEFAULT_CLI = "sb-cli"
DEFAULT_SUBSET = "swe-bench_lite"
DEFAULT_SPLIT = "test"
# OPERATOR NOTE (WS-1, best-effort — not diagnosed further here, no invocation
# behaviour changed by this note): every historical run against this exact
# (DEFAULT_SUBSET, DEFAULT_SPLIT) pair — 7/7 runs, including the Phase-1 pilot
# and the slice4 forensic re-grade's gold control — fast-failed in ~103s with
# completed_instances=0 for EVERY submitted instance, never reaching the
# hidden-test phase. That is too fast and too uniform across unrelated repos
# to be per-instance solve/patch quality; it reads as an invocation/config
# problem upstream of grading. Unconfirmed candidates an operator should check
# next (roughly cheapest-to-rule-out first): (1) the installed ``sb-cli``
# package version vs. what the swebench.com service currently expects;
# (2) whether "swe-bench_lite" / "test" are still the LIVE subset/split ids
# for that service (vs. a renamed, retired, or re-versioned split);
# (3) ``SWEBENCH_API_KEY`` entitlement/quota on this subset specifically (a
# general auth failure would more plausibly reject the submission itself
# rather than accept it and then fast-fail at ~103s); (4) a swebench.com-side
# outage/incident spanning the runs to date. The ERROR-not-FAIL reclassification
# below is what makes the benchmark signal safe regardless of which of these
# turns out to be the cause.
# sb-cli submit with --gen_report 1 blocks until the cloud evaluation finishes,
# so this is a generous ceiling, not a typical wait.
DEFAULT_SUBMIT_TIMEOUT = 1800

# The three keys of a SWE-bench prediction record, in canonical order.
_PREDICTION_KEYS = ("instance_id", "model_name_or_path", "model_patch")

# A runner runs one command and returns its result; matches ``solve._run``.
Runner = Callable[..., _SubprocessResult]


def _has_patch(prediction: Mapping[str, Any]) -> bool:
    """True iff the prediction carries a non-empty (non-whitespace) patch."""
    patch = prediction.get("model_patch")
    return bool(patch and str(patch).strip())


def _normalise(prediction: Mapping[str, Any]) -> dict[str, str]:
    """Coerce a prediction to the exact SWE-bench triple (strings only)."""
    return {
        "instance_id": str(prediction.get("instance_id", "")),
        "model_name_or_path": str(prediction.get("model_name_or_path", "")),
        "model_patch": str(prediction.get("model_patch", "")),
    }


def _locate_report(output_dir: Path, run_id: str) -> Path | None:
    """Find the report ``sb-cli --gen_report 1`` wrote under ``output_dir``.

    Prefers the ``<run_id>.json`` name; otherwise falls back to the newest
    ``*.json`` in the directory (``predictions.jsonl`` is ``.jsonl`` so it is
    never matched). Returns ``None`` when no report is present.
    """
    direct = output_dir / f"{run_id}.json"
    if direct.is_file():
        return direct
    candidates = [p for p in output_dir.glob("*.json") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@dataclass(frozen=True)
class _ParsedReport:
    """The sb-cli report fields the verdict mapping reads (see module docstring).

    ``completed`` is the report-wide ``completed_instances`` count (a batch-level
    signal): when it is 0 the WHOLE submitted batch never got past the cloud's
    pre-test fast-fail (the slice4 forensic finding), so nothing in it can be a
    genuine FAIL. ``failed`` is the report's own ``failed_ids`` — instances
    sb-cli itself flags as not having completed; this is a PER-INSTANCE signal
    that stays ERROR even inside an otherwise-completed batch, independent of
    the batch-wide ``completed`` count.
    """

    resolved: set[str]
    errored: set[str]
    unresolved: set[str]
    failed: set[str]
    completed: int


def _parse_report(report_path: Path) -> _ParsedReport:
    """Parse a sb-cli report into the fields the verdict mapping needs.

    Raises ``ValueError`` on malformed JSON / missing ``resolved_ids`` so the
    caller degrades the affected instances to ERROR (never a silent all-FAIL).
    All other fields default defensively (missing/malformed → empty set / 0)
    so an older or partial report shape never crashes the scorer — it just
    reads as "nothing completed", which is the safe (ERROR-biased) default.
    """
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable sb-cli report {report_path}: {exc}") from exc
    if not isinstance(data, Mapping) or "resolved_ids" not in data:
        raise ValueError(f"sb-cli report missing resolved_ids: {report_path}")
    resolved = {str(i) for i in (data.get("resolved_ids") or [])}
    errored = {str(i) for i in (data.get("error_ids") or [])}
    unresolved = {str(i) for i in (data.get("unresolved_ids") or [])}
    failed = {str(i) for i in (data.get("failed_ids") or [])}
    completed_raw = data.get("completed_instances", data.get("completed", 0))
    try:
        completed = int(completed_raw or 0)
    except (TypeError, ValueError):
        completed = 0
    return _ParsedReport(
        resolved=resolved,
        errored=errored,
        unresolved=unresolved,
        failed=failed,
        completed=completed,
    )


class SbcliScorer:
    """Cloud scorer that submits predictions via ``sb-cli`` and parses the report.

    Implements the :class:`~benchmarks.scorers.base.Scorer` protocol.
    """

    name = "sb-cli-swebench-lite"

    def __init__(
        self,
        *,
        runner: Runner = _run,
        cli: str = DEFAULT_CLI,
        subset: str = DEFAULT_SUBSET,
        split: str = DEFAULT_SPLIT,
        timeout: int = DEFAULT_SUBMIT_TIMEOUT,
        api_key_env: str = API_KEY_ENV,
        workdir: Path | None = None,
    ) -> None:
        self._runner = runner
        self._cli = cli
        self._subset = subset
        self._split = split
        self._timeout = timeout
        self._api_key_env = api_key_env
        # When set, artifacts (predictions.jsonl + report) are kept here for
        # inspection (the pilot); otherwise a temp dir is used and cleaned up.
        self._workdir = workdir

    # -- Scorer protocol ----------------------------------------------------

    def score(
        self, predictions: Sequence[Mapping[str, Any]], *, run_id: str
    ) -> ScoreReport:
        preds = list(predictions)

        # 1. Split off empty-patch predictions: a no-op is ERROR, never a FAIL,
        #    and is not submitted to the cloud.
        with_patch = [p for p in preds if _has_patch(p)]
        empty_scores = [
            InstanceScore(
                instance_id=str(p.get("instance_id", "")),
                status=ERROR,
                detail="empty model_patch (no source change to score)",
            )
            for p in preds
            if not _has_patch(p)
        ]

        # 2. Nothing real to score -> do not touch the network at all.
        if not with_patch:
            return ScoreReport(
                instances=list(empty_scores),
                summary={"run_id": run_id, "submitted": 0, "reason": "no patches"},
            )

        # 3. API key is required to submit; its absence is a config ERROR.
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            reason = f"{self._api_key_env} not set (cannot submit to sb-cli)"
            return ScoreReport(
                instances=[
                    *(
                        InstanceScore(_normalise(p)["instance_id"], ERROR, reason)
                        for p in with_patch
                    ),
                    *empty_scores,
                ],
                summary={"run_id": run_id, "submitted": 0, "reason": reason},
            )

        # 4. Submit + parse inside a workdir (kept if configured, else temp).
        own_tmp = self._workdir is None
        workdir = Path(self._workdir) if self._workdir else Path(tempfile.mkdtemp())
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            submitted_scores, summary = self._submit_and_parse(
                with_patch, run_id=run_id, workdir=workdir
            )
        finally:
            if own_tmp:
                shutil.rmtree(workdir, ignore_errors=True)

        return ScoreReport(
            instances=[*submitted_scores, *empty_scores], summary=summary
        )

    # -- internals ----------------------------------------------------------

    def _submit_and_parse(
        self,
        predictions: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        workdir: Path,
    ) -> tuple[list[InstanceScore], dict[str, Any]]:
        """Write predictions.jsonl, invoke sb-cli, parse the report.

        Any infra failure (non-zero exit, raised runner, missing/unparseable
        report) turns EVERY submitted instance into ERROR with the reason —
        never FAIL. Only a real report produces PASS/FAIL.
        """
        preds_path = workdir / "predictions.jsonl"
        self._write_predictions(predictions, preds_path)
        ids = [_normalise(p)["instance_id"] for p in predictions]

        cmd = [
            self._cli,
            "submit",
            self._subset,
            self._split,
            "--predictions_path",
            str(preds_path),
            "--run_id",
            run_id,
            "--gen_report",
            "1",
            "--output_dir",
            str(workdir),
        ]

        def _all_error(reason: str) -> tuple[list[InstanceScore], dict[str, Any]]:
            scores = [InstanceScore(i, ERROR, reason) for i in ids]
            return scores, {
                "run_id": run_id,
                "submitted": len(ids),
                "error": reason,
            }

        # Invoke sb-cli. A raised runner (e.g. sb-cli not installed) is ERROR.
        try:
            result = self._runner(cmd, cwd=workdir, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - any spawn failure is ERROR
            return _all_error(f"sb-cli invocation failed: {exc}")

        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-500:]
            return _all_error(
                f"sb-cli submit exited {result.returncode}: {tail or 'no output'}"
            )

        # Zero exit — locate + parse the report. Missing/malformed is ERROR.
        report_path = _locate_report(workdir, run_id)
        if report_path is None:
            return _all_error("sb-cli reported success but wrote no report")
        try:
            parsed = _parse_report(report_path)
        except ValueError as exc:
            return _all_error(str(exc))

        # Verdict mapping (see module docstring): resolved -> PASS; error_ids ->
        # ERROR; genuinely completed (completed > 0) AND explicitly unresolved AND
        # not itself flagged failed_ids -> FAIL (the ONLY FAIL path); anything
        # else -- failed_ids, or simply absent from a completed result -- is an
        # infra non-completion, so it is ERROR, never folded into FAIL.
        scores: list[InstanceScore] = []
        for instance_id in ids:
            if instance_id in parsed.resolved:
                scores.append(InstanceScore(instance_id, PASS, "resolved"))
            elif instance_id in parsed.errored:
                scores.append(
                    InstanceScore(
                        instance_id, ERROR, "sb-cli reported an evaluation error"
                    )
                )
            elif (
                parsed.completed > 0
                and instance_id in parsed.unresolved
                and instance_id not in parsed.failed
            ):
                scores.append(InstanceScore(instance_id, FAIL, "unresolved"))
            else:
                scores.append(
                    InstanceScore(
                        instance_id,
                        ERROR,
                        "sb-cli eval did not complete (infra)",
                    )
                )

        summary = {
            "run_id": run_id,
            "submitted": len(ids),
            "completed": parsed.completed,
            "resolved": sum(1 for s in scores if s.status == PASS),
            "unresolved": sum(1 for s in scores if s.status == FAIL),
            "errored": sum(1 for s in scores if s.status == ERROR),
            "report_path": str(report_path),
        }
        return scores, summary

    @staticmethod
    def _write_predictions(
        predictions: Sequence[Mapping[str, Any]], path: Path
    ) -> None:
        """Write predictions.jsonl: one JSON object per line, exactly the triple."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(_normalise(p)) for p in predictions]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_scorer(args: Any) -> SbcliScorer:
    """Construct the real (network-backed) scorer from CLI ``args``.

    Reads optional attributes defensively so a bare ``argparse.Namespace`` (the
    external CLI's parser) works with all defaults. When ``args`` carries an
    ``out_dir`` (the pilot's ``--out-dir``), the scorer's ``workdir`` is wired to
    ``out_dir/"sbcli"`` so ``predictions.jsonl`` and the raw ``<run_id>.json``
    sb-cli report are PERSISTED there for forensic inspection, instead of a temp
    dir that ``score()`` deletes when it owns it (see ``score()``'s ``own_tmp``
    branch). Without an ``out_dir`` (e.g. a bare ``argparse.Namespace()`` in a
    test), ``workdir`` stays ``None`` and behaviour is unchanged.
    """
    cli = getattr(args, "sbcli", None) or DEFAULT_CLI
    subset = getattr(args, "sbcli_subset", None) or DEFAULT_SUBSET
    split = getattr(args, "sbcli_split", None) or DEFAULT_SPLIT
    timeout = getattr(args, "sbcli_timeout", None) or DEFAULT_SUBMIT_TIMEOUT
    out_dir = getattr(args, "out_dir", None)
    workdir = Path(out_dir) / "sbcli" if out_dir else None
    return SbcliScorer(
        cli=str(cli),
        subset=str(subset),
        split=str(split),
        timeout=int(timeout),
        workdir=workdir,
    )
