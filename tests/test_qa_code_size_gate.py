"""Phase 1 gate behavior tests.

Asserts that :func:`qa.code_size.run_code_size`:

* Emits ``severity="warn"`` (NOT ``"block"``) when thresholds breach.
* Returns ``passed=True`` regardless of warnings (warn-only contract).
* Surfaces structured ``metrics`` for downstream consumers.
* Honors ``edit_scope`` filter (intersection with ``paths``).
* Returns ``severity="info"`` and empty warnings on lean fixtures.
* Degrades gracefully when no Python files are in scope.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from qa.code_size import run_code_size


_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "anti_bloat"


@pytest.fixture
def repo_with_lean_pair_01(tmp_path: Path) -> Path:
    """Copy the lean version of pair_01 into a tmp repo as src/double.py."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(
        _FIXTURES_DIR / "pair_01_speculative_abstraction.lean.py",
        src / "double.py",
    )
    return tmp_path


@pytest.fixture
def repo_with_verbose_pair_01(tmp_path: Path) -> Path:
    """Copy the verbose version of pair_01 into a tmp repo as src/double.py."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(
        _FIXTURES_DIR / "pair_01_speculative_abstraction.py",
        src / "double.py",
    )
    return tmp_path


@pytest.fixture
def repo_with_long_function(tmp_path: Path) -> Path:
    """A repo containing a function that exceeds the loc_per_function threshold."""
    src = tmp_path / "src"
    src.mkdir()
    body = "\n".join(f"    x_{i} = {i}" for i in range(150))
    (src / "long_fn.py").write_text(
        f'"""Long function file."""\n\n\ndef long_one():\n{body}\n    return x_0\n'
    )
    return tmp_path


@pytest.mark.asyncio
async def test_lean_pair_01_passes_without_warn(repo_with_lean_pair_01: Path) -> None:
    """Lean fixtures should produce a clean info-severity result."""
    result = await run_code_size(
        repo_with_lean_pair_01,
        paths=[Path("src/double.py")],
    )
    assert result.passed is True
    assert result.severity == "info"
    # No warning words in the details.
    assert "warning" not in result.details.lower()


@pytest.mark.asyncio
async def test_verbose_pair_01_emits_warn_severity(
    repo_with_verbose_pair_01: Path,
) -> None:
    """Verbose pair_01 should NOT trip thresholds (it's small overall) but
    when we use a strict per-function LOC threshold it should warn.

    The default thresholds are Fontana 2015 (cc>20, loc>100). Pair_01's
    methods are tiny — the pair illustrates abstraction count, not
    cyclomatic complexity. So we lower thresholds to surface a warn here."""
    result = await run_code_size(
        repo_with_verbose_pair_01,
        paths=[Path("src/double.py")],
        thresholds={
            "cyclomatic_max": 1,  # any file with cc>1 trips
            "loc_per_function": 100,
            "dead_symbols": 0,
            "commented_out_blocks": 0,
            "duplicate_clusters": 0,
        },
    )
    # Verbose pair_01 has an `if/raise` ⇒ cc=2 in DoublerFactory.create —
    # tripping our super-strict cc threshold of 1.
    assert result.passed is True
    assert result.severity == "warn"
    assert "cyclomatic_max" in result.details


@pytest.mark.asyncio
async def test_long_function_warning_surfaces(
    repo_with_long_function: Path,
) -> None:
    """A function with LOC=150 should land in the long_functions list and
    surface in the warn details."""
    result = await run_code_size(
        repo_with_long_function,
        paths=[Path("src/long_fn.py")],
    )
    assert result.severity == "warn"
    assert "long_one" in result.details
    assert "loc=" in result.details
    # Structured metrics carry the long-function list too.
    assert any("long_one" in entry for entry in result.metrics["long_functions"])


@pytest.mark.asyncio
async def test_passed_is_true_for_warn(repo_with_long_function: Path) -> None:
    """Warn-only contract: ``passed`` must be True even when warnings fire,
    so the orchestrator does not halt the task."""
    result = await run_code_size(
        repo_with_long_function,
        paths=[Path("src/long_fn.py")],
    )
    assert result.severity == "warn"
    assert result.passed is True


@pytest.mark.asyncio
async def test_metrics_dict_populated(repo_with_verbose_pair_01: Path) -> None:
    """Structured metrics must include the Bohr §3.4 quad keys."""
    result = await run_code_size(
        repo_with_verbose_pair_01,
        paths=[Path("src/double.py")],
    )
    for key in (
        "token_count",
        "defensive_ratio",
        "doc_density",
        "functions_per_file",
        "loc_executable",
        "cyclomatic_max",
        "n_abstractions",
        "files_measured",
        "thresholds_applied",
    ):
        assert key in result.metrics, f"missing metrics key: {key}"
    assert result.metrics["files_measured"] == 1


@pytest.mark.asyncio
async def test_empty_paths_returns_silent_pass(tmp_path: Path) -> None:
    """An empty paths list (developer made no Python edits) returns info."""
    result = await run_code_size(tmp_path, paths=[])
    assert result.passed is True
    assert result.severity == "info"
    assert "no Python files" in result.details


@pytest.mark.asyncio
async def test_edit_scope_filter_intersects_with_paths(tmp_path: Path) -> None:
    """edit_scope=['src/'] + paths=[in_scope, out_of_scope] keeps only the
    in-scope file — this prevents the gate from measuring out-of-scope writes."""
    src = tmp_path / "src"
    src.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    body = "\n".join(f"    x_{i} = {i}" for i in range(150))
    (src / "in_scope.py").write_text(
        f"def long_in_scope():\n{body}\n    return 0\n"
    )
    (other / "out_scope.py").write_text(
        f"def long_out_scope():\n{body}\n    return 0\n"
    )
    result = await run_code_size(
        tmp_path,
        paths=[Path("src/in_scope.py"), Path("other/out_scope.py")],
        edit_scope=["src"],
    )
    # Only the in-scope long fn should appear.
    long_text = result.details
    assert "long_in_scope" in long_text
    assert "long_out_scope" not in long_text
    assert result.metrics["files_measured"] == 1


@pytest.mark.asyncio
async def test_non_python_diff_files_filtered(tmp_path: Path) -> None:
    """A diff with only .md files yields a no-op pass."""
    (tmp_path / "README.md").write_text("# hi\n")
    result = await run_code_size(
        tmp_path, paths=[Path("README.md")]
    )
    assert result.passed is True
    assert result.severity == "info"


@pytest.mark.asyncio
async def test_full_tree_walk_when_paths_none(tmp_path: Path) -> None:
    """paths=None should fall back to a full-tree walk under cwd."""
    (tmp_path / "module.py").write_text(
        '"""Module."""\n\n\ndef f():\n    return 1\n'
    )
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "stale.py").write_text("def stale(): pass\n")
    result = await run_code_size(tmp_path, paths=None)
    # __pycache__ should be skipped → only module.py is measured.
    assert result.metrics["files_measured"] == 1


@pytest.mark.asyncio
async def test_thresholds_accept_pydantic_model(
    repo_with_long_function: Path,
) -> None:
    """``thresholds`` accepts a CodeSizeThresholds pydantic model via
    ``.model_dump()`` so the orchestrator can pass cfg directly."""
    from config.schema import CodeSizeThresholds

    t = CodeSizeThresholds(loc_per_function=50)
    result = await run_code_size(
        repo_with_long_function,
        paths=[Path("src/long_fn.py")],
        thresholds=t,
    )
    assert result.severity == "warn"


@pytest.mark.asyncio
async def test_lean_corpus_passes_with_default_thresholds(tmp_path: Path) -> None:
    """The full set of lean fixtures (post-refactor versions) under default
    Fontana 2015 thresholds should NOT trip a warn — they're idiomatic and
    lean. This is the canary for false-positives."""
    src = tmp_path / "src"
    src.mkdir()
    paths = []
    for lean_path in sorted(_FIXTURES_DIR.glob("*.lean.py")):
        target = src / lean_path.name.replace(".lean.py", ".py")
        shutil.copy(lean_path, target)
        paths.append(Path("src") / target.name)
    result = await run_code_size(tmp_path, paths=paths)
    assert result.severity == "info", (
        f"lean corpus tripped a warn (false-positive): {result.details}"
    )
