"""v0.37.0 H5: hallucination_guard auto-skip on huge C/C++ repos.

When :func:`orchestrator.repo_size.is_huge_repo` returns True AND the
language profile shows ≥80% C/C++, the guard's built-in skip set is
unioned with the H5 engine-shape directory names
(``PrecompiledHeaders``, ``Generated``, ``Intermediate``, ``Engine``,
``build``, plus the existing baseline). The auto-set is additive — it
never replaces operator-supplied ``hallucination_guard_skip_dirs``.

Small repos and Python-dominant huge repos see no change.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from qa.hallucination_guard import (
    _HUGE_CPP_SKIP_DIR_NAMES,
    _HUGE_CPP_SKIP_PATTERNS,
    run_hallucination_guard,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _FakeCfg:
    def __init__(self, *, disabled: bool = False) -> None:
        self.huge_repo_overrides_disabled = disabled
        # Below the default 5000 threshold for our small fixtures so the
        # H5 helper key still resolves correctly.
        self.index_full_rebuild_threshold_files = 5000


@pytest.fixture(autouse=True)
def _clear_repo_size_cache() -> None:
    from orchestrator.repo_size import clear_cache

    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# Module-level shape assertions.
# ---------------------------------------------------------------------------


def test_h5_patterns_cover_expected_engine_dirs() -> None:
    """The published patterns must include the six trees the plan calls out."""
    expected_patterns = {
        "**/PrecompiledHeaders/**",
        "**/Generated/**",
        "**/Intermediate/**",
        "**/Engine/**",
        "**/build/**",
        "**/cmake-build-*/**",
    }
    assert expected_patterns.issubset(_HUGE_CPP_SKIP_PATTERNS)


def test_h5_skip_dir_names_extracted_from_patterns() -> None:
    """Bare directory names must be derivable from the published patterns."""
    expected_names = {
        "PrecompiledHeaders",
        "Generated",
        "Intermediate",
        "Engine",
        "build",
    }
    assert expected_names.issubset(_HUGE_CPP_SKIP_DIR_NAMES)


# ---------------------------------------------------------------------------
# Small-repo backward compat: no H5 skip extension.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_repo_keeps_baseline_skip_list(tmp_path: Path) -> None:
    """A small Python repo behaves identically to pre-H5."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "Engine" / "should_be_scanned.py",
        "from os import nonexistent_func\n",
    )

    # Small repo (< 5000 files) → is_huge_repo is False → no auto-skip.
    out = await run_hallucination_guard(tmp_path, cfg=_FakeCfg())

    # The "Engine" dir is NOT in the baseline _SKIP_DIRS, so the finding
    # is visible.
    assert out.passed is False
    assert "nonexistent_func" in (out.details or "")


# ---------------------------------------------------------------------------
# Huge C/C++ repo: H5 auto-skip extends the built-in set.
# ---------------------------------------------------------------------------


@pytest.fixture
def _force_huge_cpp_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the H5 helpers so an arbitrary tmp_path looks like a huge
    C/C++ repo without having to write 5000+ .cpp files."""
    import orchestrator.repo_size as size_mod
    import runtime.language_profile as lp_mod

    def _huge(cwd: Path, threshold: int | None = None, *, cfg: Any = None) -> bool:  # noqa: ARG001
        if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
            return False
        return True

    def _cpp_profile(cwd: Path, *, force_recompute: bool = False) -> dict[str, float]:  # noqa: ARG001
        return {"cpp": 0.85, "python": 0.15}

    monkeypatch.setattr(size_mod, "is_huge_repo", _huge)
    monkeypatch.setattr(lp_mod, "compute_language_profile", _cpp_profile)


@pytest.mark.asyncio
async def test_huge_cpp_repo_auto_skips_engine_paths(
    _force_huge_cpp_repo: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Huge + C/C++ ≥80% → built-in skip list extended with H5 patterns."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "Engine" / "core.py",
        "from os import nonexistent_func\n",
    )
    _write(
        tmp_path / "Generated" / "gen.py",
        "from os import another_nonexistent\n",
    )

    with caplog.at_level(logging.INFO, logger="qa.hallucination_guard"):
        out = await run_hallucination_guard(tmp_path, cfg=_FakeCfg())

    # H5 auto-skip excludes the "bad" findings under Engine/ and Generated/.
    assert out.passed is True

    # The auto-skip log line was emitted.
    messages = " ".join(rec.message for rec in caplog.records)
    assert "huge_repo_cpp_paths_included" in messages
    assert "Engine" in messages or "Generated" in messages


@pytest.mark.asyncio
async def test_huge_cpp_repo_auto_skip_is_additive(
    _force_huge_cpp_repo: None, tmp_path: Path
) -> None:
    """``extra_skip_dirs`` is UNIONed with the H5 auto-set — not replaced."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "Engine" / "core.py",
        "from os import nonexistent_func\n",
    )
    _write(
        tmp_path / "MyVendor" / "vend.py",
        "from os import another_nonexistent\n",
    )

    out = await run_hallucination_guard(
        tmp_path, extra_skip_dirs=["MyVendor"], cfg=_FakeCfg()
    )

    # Both operator-supplied "MyVendor" AND H5 auto "Engine" are skipped.
    assert out.passed is True


# ---------------------------------------------------------------------------
# Huge Python-dominant repo: H5 auto-skip does NOT fire.
# ---------------------------------------------------------------------------


@pytest.fixture
def _force_huge_python_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Huge but Python-dominant: H5 auto-skip should NOT engage."""
    import orchestrator.repo_size as size_mod
    import runtime.language_profile as lp_mod

    def _huge(cwd: Path, threshold: int | None = None, *, cfg: Any = None) -> bool:  # noqa: ARG001
        if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
            return False
        return True

    def _python_profile(cwd: Path, *, force_recompute: bool = False) -> dict[str, float]:  # noqa: ARG001
        return {"python": 0.90, "cpp": 0.10}

    monkeypatch.setattr(size_mod, "is_huge_repo", _huge)
    monkeypatch.setattr(lp_mod, "compute_language_profile", _python_profile)


@pytest.mark.asyncio
async def test_huge_python_repo_skips_no_h5_paths(
    _force_huge_python_repo: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Huge + 90% Python → H5 auto-skip does NOT engage."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "Engine" / "core.py",
        "from os import nonexistent_func\n",
    )

    with caplog.at_level(logging.INFO, logger="qa.hallucination_guard"):
        out = await run_hallucination_guard(tmp_path, cfg=_FakeCfg())

    # The bad finding under Engine/ is visible — H5 set NOT applied.
    assert out.passed is False
    assert "nonexistent_func" in (out.details or "")
    # The auto-skip log line was NOT emitted.
    messages = " ".join(rec.message for rec in caplog.records)
    assert "huge_repo_cpp_paths_included" not in messages


# ---------------------------------------------------------------------------
# Escape hatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_hatch_disables_huge_cpp_auto_skip(
    _force_huge_cpp_repo: None, tmp_path: Path
) -> None:
    """``cfg.huge_repo_overrides_disabled=True`` → H5 auto-skip OFF."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "Engine" / "core.py",
        "from os import nonexistent_func\n",
    )

    out = await run_hallucination_guard(tmp_path, cfg=_FakeCfg(disabled=True))

    # Escape hatch flips is_huge_repo → False → no auto-skip → finding visible.
    assert out.passed is False
