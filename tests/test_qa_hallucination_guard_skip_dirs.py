"""v0.26.1 patch B: vendored-tree skip behavior for hallucination_guard.

The guard's whole-tree walk previously visited ``External/``, ``Tools/``,
``vendor/``, ``third_party/`` and ``third-party/`` (the conventional
vendor-tree names). On the 2026-05-11 Unity / SDL2 surface these contained
Latin-1 bytes that crashed the gate before patch A landed; even with
patch A the walk wastes 10-100x of the necessary scan time on copies of
upstream code the developer never edits.

The default ``_SKIP_DIRS`` constant now lists those names. Operators
with non-vendor ``External/`` (rare) can extend the list via
``cfg.qa_gates.hallucination_guard_skip_dirs`` — that list is appended
to the default set, never replacing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.hallucination_guard import run_hallucination_guard


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vendor_dir",
    ["External", "Tools", "vendor", "third_party", "third-party"],
)
async def test_skip_dirs_excludes_vendored_trees_by_default(
    tmp_path: Path, vendor_dir: str
) -> None:
    """A bad file under a vendored dir is invisible to the default scan."""
    # A clean file at the top-level keeps the walk's "did we find files?"
    # check honest.
    _write(tmp_path / "src" / "good.py", "import os\n")
    # The vendored file would crash on patch B's absence (no skip) +
    # patch A's absence (no errors=replace). With patch B it's never
    # opened.
    _write(
        tmp_path / vendor_dir / "bad.py",
        "from os import nonexistent_func\n",
    )

    out = await run_hallucination_guard(tmp_path)

    assert out.passed is True


@pytest.mark.asyncio
async def test_skip_dirs_config_extension(tmp_path: Path) -> None:
    """``extra_skip_dirs`` extends (does NOT replace) the default set."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "MyVendor" / "bad.py",
        "from os import nonexistent_func\n",
    )

    out = await run_hallucination_guard(
        tmp_path, extra_skip_dirs=["MyVendor"]
    )

    assert out.passed is True


@pytest.mark.asyncio
async def test_skip_dirs_extension_does_not_remove_defaults(
    tmp_path: Path,
) -> None:
    """When extra_skip_dirs is set, the default External skip still fires."""
    _write(tmp_path / "src" / "good.py", "import os\n")
    _write(
        tmp_path / "External" / "bad.py",
        "from os import nonexistent_func\n",
    )

    out = await run_hallucination_guard(
        tmp_path, extra_skip_dirs=["MyVendor"]
    )

    # Default External skip is still in effect.
    assert out.passed is True
