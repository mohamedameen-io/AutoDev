"""Group B engagement gate: holdout must be WIRED into the live tournament.

Phase 1B / Group B — closes WS2-1-holdout-dead-and-pytest-only.

Engagement-first TDD. The holdout module (``tournament.holdout``) shipped in
v0.19.0 with full unit coverage but was *dead code*: no ``src/`` caller ever
ran ``run_holdout_tests`` / ``extract_baseline_tests``. The promotion ladder's
``repeated → promotion_eligible`` transition (core.py, the "third consecutive
win clears the holdout-equivalent step" comment) advanced WITHOUT ever
executing a holdout run — a gate that passes because it ran NOTHING.

These tests assert:

  1. ENGAGEMENT: driving the tournament promotion path to the
     ``repeated → eligible`` rung actually INVOKES the holdout run (a real
     ``HoldoutResult`` is produced + the discovery fired), not just that the
     function is importable.
  2. GREP ENGAGEMENT: ``run_holdout_tests`` is referenced in ``src/`` outside
     ``holdout.py`` (≥1 hit). This is the dead-code proof.
  3. NON-VACUITY: a holdout run over a NON-EMPTY scope that discovers 0 tests
     must NOT silently pass — it must degrade loud (mirror Group A's signal).
  4. PER-LANGUAGE discovery: Go (``*_test.go``) / TS (``*.test.ts``) fixtures
     are discovered (not pytest-only).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ── 2. GREP ENGAGEMENT PROOF (RED-on-HEAD; the dead-code assertion) ──────────


def _src_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src"


def test_holdout_is_referenced_in_src_outside_holdout_module() -> None:
    """``run_holdout_tests`` must have ≥1 src caller outside holdout.py.

    RED-on-HEAD: today the only reference is the definition inside
    ``holdout.py`` itself — the module is dead code. This is the exact
    ``grep -rn "run_holdout_tests" src/ | grep -v holdout.py`` engagement
    proof the gate G1 demands (≥1 hit).
    """
    src = _src_root()
    hits: list[str] = []
    for py in src.rglob("*.py"):
        if py.name == "holdout.py":
            continue
        if "__pycache__" in py.parts:
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        if "run_holdout_tests" in text:
            hits.append(str(py.relative_to(src)))
    assert hits, (
        "holdout is DEAD CODE: no src caller references run_holdout_tests "
        "outside holdout.py. Expected >=1 hit (the live-tournament wire)."
    )


# ── 3. NON-VACUITY: 0 tests in a non-empty scope must degrade LOUD ──────────


@pytest.mark.asyncio
async def test_holdout_zero_tests_in_nonempty_scope_is_loud_not_silent_pass() -> (
    None
):
    """A scope with source files but ZERO discoverable tests must NOT pass.

    This is the canonical "gate found nothing → passes" bug. The fix must
    surface a loud signal (``passed=False`` + a diagnostic mentioning the
    empty discovery), mirroring Group A's no-silent-dead-end invariant.
    """
    from tournament.holdout import discover_holdout_scope

    repo = Path(__file__)  # placeholder; real dir injected below
    # Build a non-empty scope (source files) but with NO tests.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "main.go").write_text("package main\nfunc main() {}\n")
        (d / "lib.rs").write_text("pub fn add(a: i32) -> i32 { a }\n")
        result = await discover_holdout_scope(d)
        # Non-empty scope, zero tests discovered → must be LOUD.
        assert not result.passed, (
            "holdout vacuously PASSED on a non-empty scope with 0 tests — "
            "this is the found-nothing-so-pass bug."
        )
        assert result.test_count == 0
        assert "no tests" in result.failure_summary.lower() or (
            "0 test" in result.failure_summary.lower()
        ), f"expected a loud diagnostic, got: {result.failure_summary!r}"
        _ = repo


@pytest.mark.asyncio
async def test_holdout_empty_scope_truly_empty_is_vacuous_pass() -> None:
    """An empty scope (no source, no tests) is a legitimate vacuous pass.

    Contrast with the non-empty/zero-tests case: a genuinely empty repo has
    nothing to regress, so it should NOT be treated as a found-nothing bug.
    """
    from tournament.holdout import discover_holdout_scope
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        result = await discover_holdout_scope(Path(td))
        assert result.passed


# ── 4. PER-LANGUAGE DISCOVERY (not pytest-only) ─────────────────────────────


@pytest.mark.asyncio
async def test_holdout_discovers_go_tests() -> None:
    """A Go ``*_test.go`` fixture is discovered (Go support, not pytest-only)."""
    from tournament.holdout import discover_holdout_scope
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "main.go").write_text("package main\nfunc main() {}\n")
        (d / "main_test.go").write_text(
            "package main\nimport \"testing\"\n"
            "func TestX(t *testing.T) {}\n"
        )
        result = await discover_holdout_scope(d)
        assert result.test_count >= 1, "Go *_test.go not discovered"


@pytest.mark.asyncio
async def test_holdout_discovers_ts_tests() -> None:
    """A TS ``*.test.ts`` fixture is discovered (TS support, not pytest-only)."""
    from tournament.holdout import discover_holdout_scope
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "index.ts").write_text("export const x = 1;\n")
        (d / "index.test.ts").write_text(
            "test('x', () => { expect(1).toBe(1); });\n"
        )
        result = await discover_holdout_scope(d)
        assert result.test_count >= 1, "TS *.test.ts not discovered"


@pytest.mark.asyncio
async def test_holdout_discovers_rust_tests() -> None:
    """A Rust ``#[test]`` fixture is discovered (Rust support, not pytest-only)."""
    from tournament.holdout import discover_holdout_scope
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "lib.rs").write_text(
            "pub fn add(a: i32) -> i32 { a }\n"
            "#[cfg(test)]\nmod tests {\n#[test]\nfn it_works() {}\n}\n"
        )
        result = await discover_holdout_scope(d)
        assert result.test_count >= 1, "Rust #[test] not discovered"


# ── 1. LIVE-TOURNAMENT ENGAGEMENT (the headline RED-on-HEAD) ────────────────


def _init_repo_with_baseline_test(tmp_path: Path, passes: bool) -> str:
    """Init a git repo with one baseline python test. Returns commit hash."""
    subprocess.check_call(("git", "init", "-q"), cwd=tmp_path)
    subprocess.check_call(
        ("git", "config", "user.email", "t@example.com"), cwd=tmp_path
    )
    subprocess.check_call(("git", "config", "user.name", "test"), cwd=tmp_path)
    (tmp_path / "tests").mkdir()
    body = "assert True" if passes else "assert False"
    (tmp_path / "tests" / "test_baseline.py").write_text(
        f"def test_baseline(): {body}\n"
    )
    subprocess.check_call(("git", "add", "."), cwd=tmp_path)
    subprocess.check_call(("git", "commit", "-q", "-m", "baseline"), cwd=tmp_path)
    return subprocess.check_output(
        ("git", "rev-parse", "HEAD"), cwd=tmp_path, text=True
    ).strip()


@pytest.mark.asyncio
async def test_promotion_to_eligible_actually_runs_holdout(tmp_path: Path) -> None:
    """ENGAGEMENT: the repeated→eligible transition INVOKES a real holdout run.

    RED-on-HEAD: today ``_next_grade_for_non_a_win`` advances from
    ``repeated`` to ``promotion_eligible`` WITHOUT ever calling
    ``run_holdout_tests`` — the holdout-equivalent step is a no-op. We assert
    the wire fires: a ``HoldoutResult`` is produced and recorded.

    BROKEN-CONTROL: removing the wire (or setting holdout disabled) leaves
    ``last_holdout_result`` None and the gate goes red.
    """
    from tournament.core import Tournament, TournamentConfig
    from tournament.state import TournamentArtifactStore

    artifact_dir = tmp_path / "art"
    artifact_dir.mkdir()

    repo = tmp_path / "repo"
    repo.mkdir()
    commit = _init_repo_with_baseline_test(repo, passes=True)

    cfg = TournamentConfig(
        promotion_grade_enabled=True,
        holdout_enabled=True,
    )

    class _StubHandler:
        def render_as_markdown(self, x: object) -> str:  # noqa: D401
            return str(x)

    class _StubClient:
        async def call(self, **_: object) -> str:  # pragma: no cover
            return ""

    tour: Tournament[str] = Tournament(
        handler=_StubHandler(),  # type: ignore[arg-type]
        client=_StubClient(),  # type: ignore[arg-type]
        cfg=cfg,
        artifact_dir=artifact_dir,
        holdout_cwd=repo,
        holdout_baseline_commit=commit,
    )

    # Drive the ladder to the `repeated` rung so the next non-A win triggers
    # the holdout-gated `repeated → eligible` transition.
    store = TournamentArtifactStore(artifact_dir)
    store.write_incumbent_after(1, "x", grade="repeated")

    grade = await tour._next_grade_for_non_a_win_async()

    assert tour.last_holdout_result is not None, (
        "holdout NEVER ran on the repeated→eligible transition — the "
        "holdout-equivalent step is a no-op (dead code)."
    )
    assert tour.last_holdout_result.test_count >= 1, (
        "holdout ran but discovered no baseline tests — discovery did not fire"
    )
    assert grade == "promotion_eligible"


@pytest.mark.asyncio
async def test_failing_holdout_blocks_promotion_in_live_tournament(
    tmp_path: Path,
) -> None:
    """A FAILING baseline test blocks the repeated→eligible promotion live.

    Proves the wire is load-bearing: the holdout result actually gates the
    rung, it is not merely logged.
    """
    from tournament.core import Tournament, TournamentConfig
    from tournament.state import TournamentArtifactStore

    artifact_dir = tmp_path / "art"
    artifact_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    commit = _init_repo_with_baseline_test(repo, passes=False)

    cfg = TournamentConfig(promotion_grade_enabled=True, holdout_enabled=True)

    class _StubHandler:
        def render_as_markdown(self, x: object) -> str:
            return str(x)

    class _StubClient:
        async def call(self, **_: object) -> str:  # pragma: no cover
            return ""

    tour: Tournament[str] = Tournament(
        handler=_StubHandler(),  # type: ignore[arg-type]
        client=_StubClient(),  # type: ignore[arg-type]
        cfg=cfg,
        artifact_dir=artifact_dir,
        holdout_cwd=repo,
        holdout_baseline_commit=commit,
    )
    store = TournamentArtifactStore(artifact_dir)
    store.write_incumbent_after(1, "x", grade="repeated")

    grade = await tour._next_grade_for_non_a_win_async()
    assert tour.last_holdout_result is not None
    assert not tour.last_holdout_result.passed
    # Failing holdout must NOT advance to eligible — stays at `repeated`.
    assert grade == "repeated", (
        "failing holdout did not block promotion — the wire is not load-bearing"
    )
