"""Gate tests for the coarse SWE-bench regression tripwire (Phase-1 P1.5).

The coarse gate is the on-demand red/green verdict over a fixed ~15-20
SWE-bench-Lite slice. It compares the current run's resolve-rate against a stored
Phase-1 baseline and, crucially for this project's recurring failure mode
(``test-green-but-field-inert`` / ``gates-pass-on-nothing``), it must be
**non-vacuous**: a degenerate or empty slice is RED, never a silent green.

These pins mirror the anti-vacuity + broken-control discipline of
``ci/release_preflight_greps.sh::preflight_v100`` and encode the six required
proofs from the plan
(``thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md`` P1.5):

  * a big resolve-rate DROP (>= the coarse threshold) vs baseline -> RED;
  * ``completed < min`` (too few real capability verdicts) -> RED;
  * ``ERROR-rate > threshold`` (a flaky/quota-degenerate slice) -> RED;
  * a healthy run (small/no drop, enough completed, low ERROR) -> GREEN;
  * NO baseline -> the run ESTABLISHES it and returns GREEN (no gate on run 1);
  * ``AUTODEV_BENCH_FORCE_NULL_SOLVER=1`` -> RED (the broken control that proves
    the gate can fail).

Every assertion is designed to be independently able to FAIL for its intended
reason: the RED fixtures each violate exactly ONE condition (the others held
satisfied), and the broken-control / healthy pair share the SAME healthy slice so
the only difference that flips green->red is the planted env var.

Fully in-memory: no network, no sb-cli, no baseline on the real tree (every
baseline is written under ``tmp_path``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from benchmarks.gate.coarse_gate import (
    COARSE_RESOLVE_DROP_THRESHOLD,
    DEFAULT_MAX_ERROR_RATE,
    DEFAULT_MIN_COMPLETED,
    FORCE_NULL_SOLVER_ENV,
    GREEN,
    RED,
    SLICE_NAME,
    STATUS_BASELINE_ESTABLISHED,
    STATUS_BASELINE_UNREADABLE,
    STATUS_BROKEN_CONTROL,
    STATUS_ERROR_RATE_EXCEEDED,
    STATUS_HEALTHY,
    STATUS_INSUFFICIENT_COMPLETED,
    STATUS_REGRESSED,
    CoarseGateConfig,
    GateInstance,
    evaluate_coarse_gate,
    gate_instances_from_score_report,
    write_baseline,
)
from benchmarks.scorers.base import ERROR, FAIL, PASS, InstanceScore, ScoreReport

_VERSION = "0.99.0-test"


# ---------------------------------------------------------------------------
# Fixture builders — construct slices with an EXACT PASS/FAIL/ERROR mix so each
# test isolates a single gate condition.
# ---------------------------------------------------------------------------


def _mk(
    passed: int,
    failed: int,
    errored: int,
    *,
    wall: float = 1.0,
    quota: float = 0.0,
    blind: bool = False,
) -> list[GateInstance]:
    """A slice of ``passed`` PASS, ``failed`` FAIL, ``errored`` ERROR instances."""
    out: list[GateInstance] = []
    n = 0
    for status, count in ((PASS, passed), (FAIL, failed), (ERROR, errored)):
        for _ in range(count):
            out.append(
                GateInstance(
                    instance_id=f"inst-{n:03d}",
                    status=status,
                    wall_time_s=wall,
                    # Quota waits accompany the quota-exhausted (ERROR) path.
                    quota_wait_time_s=quota if status == ERROR else 0.0,
                    blind=blind,
                )
            )
            n += 1
    return out


def _baseline_root(tmp_path: Path) -> Path:
    return tmp_path / "baselines"


def _baseline_file(tmp_path: Path, version: str = _VERSION) -> Path:
    return _baseline_root(tmp_path) / SLICE_NAME / f"{version}.json"


# A healthy baseline: 20 completed, resolve-rate 0.60 (12/20).
def _healthy_baseline() -> list[GateInstance]:
    return _mk(12, 8, 0)


# A healthy current run: 19 completed (11/19 = 0.579), 1 quota ERROR (5% error
# rate). Small drop vs the 0.60 baseline -> GREEN. Re-used by the establish and
# broken-control tests so the ONLY thing that flips green->red is the env var /
# baseline presence.
def _healthy_current() -> list[GateInstance]:
    return _mk(11, 8, 1, quota=30.0, blind=False)


def _evaluate(
    instances: Sequence[GateInstance],
    tmp_path: Path,
    *,
    version: str = _VERSION,
    config: CoarseGateConfig | None = None,
):
    return evaluate_coarse_gate(
        instances,
        baselines_root=_baseline_root(tmp_path),
        autodev_version=version,
        config=config or CoarseGateConfig(),
    )


@pytest.fixture(autouse=True)
def _clean_force_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the broken-control env var UNSET, so a green
    verdict is never accidentally flipped by the ambient environment. The
    broken-control test sets it explicitly."""
    monkeypatch.delenv(FORCE_NULL_SOLVER_ENV, raising=False)


# ---------------------------------------------------------------------------
# 1. Big resolve-rate drop (>= threshold) -> RED.
# ---------------------------------------------------------------------------


def test_big_drop_is_red(tmp_path: Path) -> None:
    """A large resolve-rate drop vs the baseline is a RED regression.

    Baseline 12/20 = 0.60; current 4/19 = 0.21 -> drop ~0.39 >= 0.25. completed
    (19) and error-rate (5%) both stay healthy, so ONLY the drop condition fires
    — if the drop check were removed this would go GREEN and fail."""
    write_baseline(_baseline_root(tmp_path), _VERSION, _healthy_baseline())

    report = _evaluate(_mk(4, 15, 1, quota=30.0), tmp_path)

    assert report.verdict == RED
    assert report.is_red is True
    assert report.status == STATUS_REGRESSED
    assert STATUS_REGRESSED in report.reasons
    assert report.resolve_rate_drop is not None
    assert report.resolve_rate_drop >= COARSE_RESOLVE_DROP_THRESHOLD
    # the other two anti-vacuity conditions did NOT fire (isolation):
    assert report.completed >= DEFAULT_MIN_COMPLETED
    assert report.error_rate <= DEFAULT_MAX_ERROR_RATE


# ---------------------------------------------------------------------------
# 2. Too few completed (real capability verdicts) -> RED (anti-vacuity).
# ---------------------------------------------------------------------------


def test_insufficient_completed_is_red(tmp_path: Path) -> None:
    """A slice with < min completed instances cannot be trusted -> RED, even
    with no baseline. It must NOT establish a baseline (a degenerate slice must
    never be blessed as the reference)."""
    # 5 PASS + 3 FAIL = 8 completed (< 12); 2 ERROR (20% < 30%). Resolve-rate
    # 0.625 is actually ABOVE the healthy baseline, so the drop condition is not
    # what fires here — only the min-completed guard.
    report = _evaluate(_mk(5, 3, 2, quota=30.0), tmp_path)

    assert report.verdict == RED
    assert report.status == STATUS_INSUFFICIENT_COMPLETED
    assert STATUS_INSUFFICIENT_COMPLETED in report.reasons
    assert report.completed < DEFAULT_MIN_COMPLETED
    # isolation: the error-rate condition did NOT fire.
    assert report.error_rate <= DEFAULT_MAX_ERROR_RATE
    # a degenerate slice must not poison the baseline.
    assert not _baseline_file(tmp_path).exists()
    assert report.baseline_established is False


# ---------------------------------------------------------------------------
# 3. ERROR-rate above threshold -> RED (anti-vacuity).
# ---------------------------------------------------------------------------


def test_error_rate_exceeded_is_red(tmp_path: Path) -> None:
    """A slice where too many instances ERRORed (infra/quota flake) is RED — an
    ERROR-heavy slice is untrustworthy, never a silent green."""
    # 8 PASS + 4 FAIL = 12 completed (== min, so the min guard does NOT fire);
    # 8 ERROR of 20 total = 0.40 error-rate (> 0.30). ONLY the error-rate fires.
    report = _evaluate(_mk(8, 4, 8, quota=30.0), tmp_path)

    assert report.verdict == RED
    assert report.status == STATUS_ERROR_RATE_EXCEEDED
    assert STATUS_ERROR_RATE_EXCEEDED in report.reasons
    assert report.error_rate > DEFAULT_MAX_ERROR_RATE
    # isolation: completed (12) is NOT below the minimum.
    assert report.completed >= DEFAULT_MIN_COMPLETED
    assert not _baseline_file(tmp_path).exists()


# ---------------------------------------------------------------------------
# 4. Healthy run -> GREEN (the control the RED tests contrast against).
# ---------------------------------------------------------------------------


def test_healthy_is_green(tmp_path: Path) -> None:
    """Enough completed, low ERROR, only a small drop vs baseline -> GREEN."""
    write_baseline(_baseline_root(tmp_path), _VERSION, _healthy_baseline())

    report = _evaluate(_healthy_current(), tmp_path)

    assert report.verdict == GREEN
    assert report.is_red is False
    assert report.status == STATUS_HEALTHY
    assert report.reasons == []
    assert report.resolve_rate_drop is not None
    assert report.resolve_rate_drop < COARSE_RESOLVE_DROP_THRESHOLD
    # the report carries the per-instance telemetry the plan requires.
    assert report.completed >= DEFAULT_MIN_COMPLETED
    assert len(report.per_instance) == 20
    row = report.per_instance[0]
    assert {"instance_id", "status", "wall_time_s", "quota_wait_time_s", "blind"} <= set(
        row
    )
    assert report.total_quota_wait_time_s == pytest.approx(30.0)  # the 1 ERROR waited


# ---------------------------------------------------------------------------
# 5. No baseline -> ESTABLISH + GREEN (no gate on run 1).
# ---------------------------------------------------------------------------


def test_missing_baseline_establishes_and_is_green(tmp_path: Path) -> None:
    """The first run over a healthy slice writes the baseline and returns GREEN
    with the ``baseline-established`` status (no comparison on run 1)."""
    assert not _baseline_file(tmp_path).exists()

    report = _evaluate(_healthy_current(), tmp_path)

    assert report.verdict == GREEN
    assert report.status == STATUS_BASELINE_ESTABLISHED
    assert report.baseline_established is True
    # the baseline was actually written to disk and is parseable.
    path = _baseline_file(tmp_path)
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["slice"] == SLICE_NAME
    assert doc["autodev_version"] == _VERSION
    assert "resolve_rate" in doc["summary"]
    assert len(doc["results"]) == 20
    # run 1 has nothing to compare against.
    assert report.baseline_resolve_rate is None
    assert report.resolve_rate_drop is None


def test_present_but_corrupt_baseline_is_red_not_green(tmp_path: Path) -> None:
    """A PRESENT-but-corrupt baseline (malformed JSON on disk) must be RED
    ``baseline-unreadable`` — NEVER silently degraded to an empty 0.0-rate baseline
    that masks a total capability collapse as a healthy green.

    Non-vacuous: the EXACT slice fed here (``_healthy_current``) is the one
    ``test_healthy_is_green`` / ``test_missing_baseline_establishes_and_is_green``
    prove GREEN. The only difference is the corrupt file on disk — a gate that
    degraded the unreadable baseline to ``{}`` (baseline_pass_rate 0.0) would call
    this GREEN 'healthy' and fail this assertion."""
    # Write a PRESENT baseline file whose bytes are not valid JSON.
    corrupt = _baseline_file(tmp_path)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ this is : not valid json ,,, ", encoding="utf-8")
    assert corrupt.is_file()

    report = _evaluate(_healthy_current(), tmp_path)

    assert report.verdict == RED
    assert report.is_red is True
    assert report.status == STATUS_BASELINE_UNREADABLE
    assert STATUS_BASELINE_UNREADABLE in report.reasons

    # Regression guard: the genuine 'no baseline exists yet' path (a version with
    # NO file) is still GREEN + establishes — a corrupt present baseline must not
    # be conflated with an absent one.
    established = _evaluate(_healthy_current(), tmp_path, version="1.0.0-absent")
    assert established.verdict == GREEN
    assert established.status == STATUS_BASELINE_ESTABLISHED
    assert established.baseline_established is True


def test_established_baseline_round_trips_into_comparison(tmp_path: Path) -> None:
    """The baseline a run establishes is a valid reference for the NEXT run:
    establish over the healthy baseline slice, then a big-drop run against it is
    RED. Proves the written baseline is actually re-loadable + comparable (not a
    write-only artefact)."""
    established = _evaluate(_healthy_baseline(), tmp_path)
    assert established.status == STATUS_BASELINE_ESTABLISHED

    # second run, same version -> compares against the just-written baseline.
    report = _evaluate(_mk(4, 15, 1, quota=30.0), tmp_path)
    assert report.verdict == RED
    assert report.status == STATUS_REGRESSED
    assert report.baseline_resolve_rate == pytest.approx(0.60, abs=1e-9)


# ---------------------------------------------------------------------------
# 6. Broken control: AUTODEV_BENCH_FORCE_NULL_SOLVER=1 -> RED.
# ---------------------------------------------------------------------------


def test_broken_control_null_solver_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE broken control (mirrors preflight_v100's forced-disable): feed the
    EXACT healthy slice + baseline that ``test_healthy_is_green`` proves GREEN,
    then set the env var — the gate MUST go RED. The only difference between this
    test and the green one is the planted env var, so a gate that ignored it
    would pass this vacuously (and this test would fail)."""
    write_baseline(_baseline_root(tmp_path), _VERSION, _healthy_baseline())
    monkeypatch.setenv(FORCE_NULL_SOLVER_ENV, "1")

    report = _evaluate(_healthy_current(), tmp_path)

    assert report.verdict == RED
    assert report.status == STATUS_BROKEN_CONTROL
    assert STATUS_BROKEN_CONTROL in report.reasons


def test_broken_control_does_not_establish_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the broken control with NO baseline, the gate is RED and must NOT
    write a (null-solver-poisoned) baseline."""
    monkeypatch.setenv(FORCE_NULL_SOLVER_ENV, "1")

    report = _evaluate(_healthy_current(), tmp_path)

    assert report.verdict == RED
    assert report.status == STATUS_BROKEN_CONTROL
    assert not _baseline_file(tmp_path).exists()
    assert report.baseline_established is False


# ---------------------------------------------------------------------------
# Named-constant sanity — the coarse tripwire must stay coarse.
# ---------------------------------------------------------------------------


def test_threshold_is_coarse_and_min_is_sane() -> None:
    """The drop threshold is sized to N~=15-20 noise (0.20-0.30 band) and the
    min-completed floor is >= 12 per the plan. A silent mis-set to a tiny
    threshold (making the tripwire hair-trigger) would fail here."""
    assert 0.20 <= COARSE_RESOLVE_DROP_THRESHOLD <= 0.30
    assert DEFAULT_MIN_COMPLETED >= 12
    assert 0.0 < DEFAULT_MAX_ERROR_RATE < 1.0


def test_empty_slice_is_red_not_green(tmp_path: Path) -> None:
    """An empty slice (0 instances) is the ultimate degenerate case: it must be
    RED (insufficient completed), never a vacuous green establish."""
    report = _evaluate([], tmp_path)
    assert report.verdict == RED
    assert report.status == STATUS_INSUFFICIENT_COMPLETED
    assert report.completed == 0
    assert not _baseline_file(tmp_path).exists()


# ---------------------------------------------------------------------------
# The wiring builder: ScoreReport (+ telemetry) -> GateInstance list.
# ---------------------------------------------------------------------------


def test_gate_instances_from_score_report_maps_status_and_telemetry() -> None:
    """The builder aligns each instance's PASS/FAIL/ERROR verdict (from the
    scorer's ScoreReport) with its wall-time / quota-wait / blind telemetry by
    instance_id, defaulting missing telemetry to zero/False."""
    sr = ScoreReport(
        instances=[
            InstanceScore("a", PASS),
            InstanceScore("b", FAIL),
            InstanceScore("c", ERROR),
        ]
    )
    built = gate_instances_from_score_report(
        sr,
        wall_times={"a": 12.5, "b": 3.0},
        quota_waits={"c": 300.0},
        blind={"b": True},
    )

    by_id = {g.instance_id: g for g in built}
    # status is carried from the ScoreReport, keyed correctly by id.
    assert by_id["a"].status == PASS
    assert by_id["b"].status == FAIL
    assert by_id["c"].status == ERROR
    # telemetry is joined by id, with sane defaults for the missing entries.
    assert by_id["a"].wall_time_s == 12.5
    assert by_id["a"].quota_wait_time_s == 0.0  # not in quota_waits -> default
    assert by_id["b"].blind is True
    assert by_id["c"].quota_wait_time_s == 300.0
    assert by_id["c"].blind is False  # not in blind map -> default


# ---------------------------------------------------------------------------
# WS-7: known network-artifact instances are excluded from the gate cohort —
# out of the resolve-rate denominator + go/no-go — and surfaced (mirrors the
# blind-instance listing at coarse_gate.py:344). psf__requests-1963 /
# psf__requests-2148 need live httpbin.org and are unpassable offline.
# ---------------------------------------------------------------------------


def test_network_artifact_instances_excluded_from_cohort_and_denominator(
    tmp_path: Path,
) -> None:
    """A cohort of 12 PASS + 8 FAIL (resolve 0.60, completed 20) with 2 EXTRA
    network-artifact FAILs must count completed=20 / resolve=0.60 — the artifacts
    are OUT of the denominator — and surface the 2 excluded ids in a dedicated
    ``network_artifact_instances`` list (like ``blind_instances``).

    RED pre-fix: without exclusion the 2 artifact FAILs inflate completed to 22
    and drop the resolve-rate to 12/22 = 0.545."""
    healthy = _mk(12, 8, 0)  # 20 completed, resolve 0.60
    artifacts = [
        GateInstance(
            instance_id="psf__requests-1963", status=FAIL, network_artifact=True
        ),
        GateInstance(
            instance_id="psf__requests-2148", status=FAIL, network_artifact=True
        ),
    ]

    report = _evaluate(healthy + artifacts, tmp_path)

    # denominator + resolve-rate are computed on the cohort ONLY (artifacts out).
    assert report.completed == 20
    assert report.resolve_rate == pytest.approx(0.60)
    assert report.passed == 12
    assert report.failed == 8  # NOT 10 — the 2 artifact FAILs are excluded
    # surfaced as an explicit excluded list (mirror of blind_instances).
    assert set(report.network_artifact_instances) == {
        "psf__requests-1963",
        "psf__requests-2148",
    }
    # the artifacts are NOT in the scored per-instance cohort ...
    per_ids = {r["instance_id"] for r in report.per_instance}
    assert "psf__requests-1963" not in per_ids
    assert "psf__requests-2148" not in per_ids
    # ... and are NOT written into the established baseline (they would poison
    # every future comparison's denominator).
    doc = json.loads(_baseline_file(tmp_path).read_text(encoding="utf-8"))
    baseline_ids = {r["instance_id"] for r in doc["results"]}
    assert "psf__requests-1963" not in baseline_ids
    assert "psf__requests-2148" not in baseline_ids
    assert doc["summary"]["completed"] == 20


def test_network_artifact_exclusion_can_flip_insufficient_completed(
    tmp_path: Path,
) -> None:
    """The exclusion is genuinely load-bearing on the go/no-go: a slice of 11
    real completed + 2 network-artifact completed is 13 raw, but only 11 in the
    cohort — BELOW the min-completed floor (12) — so the gate is RED
    ``insufficient-completed``. Counting the artifacts would vacuously clear the
    floor (13 >= 12) and mask an under-powered slice.

    RED pre-fix: with the artifacts counted, completed=13 and this run is GREEN."""
    real = _mk(7, 4, 0)  # 11 completed, resolve ~0.636
    artifacts = [
        GateInstance(
            instance_id="psf__requests-1963", status=PASS, network_artifact=True
        ),
        GateInstance(
            instance_id="psf__requests-2148", status=FAIL, network_artifact=True
        ),
    ]

    report = _evaluate(real + artifacts, tmp_path)

    assert report.completed == 11
    assert report.verdict == RED
    assert report.status == STATUS_INSUFFICIENT_COMPLETED
    assert set(report.network_artifact_instances) == {
        "psf__requests-1963",
        "psf__requests-2148",
    }
    # a degenerate (under-powered) cohort must not become a baseline.
    assert not _baseline_file(tmp_path).exists()
    assert report.baseline_established is False


def test_normal_slice_has_no_network_artifacts_and_stays_counted(
    tmp_path: Path,
) -> None:
    """Non-vacuous back-compat: a slice with NO known network-artifact ids reports
    an empty ``network_artifact_instances`` and keeps every completed verdict in
    the denominator (the exclusion never touches ordinary instances)."""
    report = _evaluate(_mk(12, 8, 0), tmp_path)
    assert report.network_artifact_instances == []
    assert report.completed == 20
    assert report.resolve_rate == pytest.approx(0.60)


def test_gate_instances_from_score_report_flags_known_network_artifacts() -> None:
    """The builder flags KNOWN network-artifact ids on the GateInstance — exactly
    how it sets ``blind`` from the blind map — so a real pilot run's gate cohort
    excludes them without the caller wiring anything. A normal id is left False."""
    from benchmarks.gate.coarse_gate import KNOWN_NETWORK_ARTIFACT_INSTANCES

    assert {"psf__requests-1963", "psf__requests-2148"} <= KNOWN_NETWORK_ARTIFACT_INSTANCES

    sr = ScoreReport(
        instances=[
            InstanceScore("psf__requests-1963", FAIL),
            InstanceScore("psf__requests-2148", FAIL),
            InstanceScore("pallets__flask-4992", PASS),
        ]
    )
    built = gate_instances_from_score_report(sr)
    by_id = {g.instance_id: g for g in built}
    assert by_id["psf__requests-1963"].network_artifact is True
    assert by_id["psf__requests-2148"].network_artifact is True
    assert by_id["pallets__flask-4992"].network_artifact is False
