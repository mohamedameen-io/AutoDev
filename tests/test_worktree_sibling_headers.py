"""v0.34.0 B2: sibling-header inclusion in sparse-checkout worktrees.

Covers four cases from the v0.34 plan:

* C/C++ source files pull their sibling `*.h` / `*.hpp` / `*.hh` /
  `*.hxx` headers into the sparse set.
* Python source files produce no header expansion.
* The expansion respects the `include_headers_for_sparse=False` opt-out.
* Expansion is skipped when proposed additions exceed the
  `WORKTREE_HEADER_EXPANSION_CAP`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch


from orchestrator.worktree import (
    WORKTREE_HEADER_EXPANSION_CAP,
    _sibling_header_paths,
)


def _init_git_repo_with(path: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(path), check=True, capture_output=True,
    )
    for rel, body in files.items():
        fp = path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(path), check=True, capture_output=True,
    )


def test_expand_sparse_paths_includes_sibling_headers_for_cpp(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "src/engine/world.cpp": "// cpp\n",
            "src/engine/world.h": "// h\n",
            "src/engine/util.hpp": "// hpp\n",
            "src/engine/leaf.hh": "// hh\n",
            "src/engine/box.hxx": "// hxx\n",
            "src/engine/other.cpp": "// unrelated\n",
        },
    )
    out = _sibling_header_paths(
        [repo / "src/engine/world.cpp"], repo, language_profile=None
    )
    assert "src/engine/world.h" in out
    assert "src/engine/util.hpp" in out
    assert "src/engine/leaf.hh" in out
    assert "src/engine/box.hxx" in out
    # cpp siblings of the source file MUST NOT be admitted as headers.
    assert "src/engine/other.cpp" not in out


def test_expand_sparse_paths_no_headers_for_python(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "src/mod/feature.py": "def f(): pass\n",
            "src/mod/feature.h": "// stray header\n",
        },
    )
    out = _sibling_header_paths(
        [repo / "src/mod/feature.py"], repo, language_profile=None
    )
    # Python source must not trigger sibling-header expansion at all.
    assert out == set()


def test_expand_sparse_paths_disabled_when_config_false(tmp_path: Path) -> None:
    """The opt-out path on ``create_per_task`` is honored.

    We assert the contract at the call-site level by patching the
    helper and confirming it is NEVER called when the flag is False.
    """
    import asyncio

    from orchestrator.worktree import WorktreeManager

    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "src/engine/world.cpp": "// cpp\n",
            "src/engine/world.h": "// h\n",
        },
    )
    wt_dir = tmp_path / "wts"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    called = {"n": 0}

    def fake_helper(*_a: object, **_k: object) -> set[str]:
        called["n"] += 1
        return {"src/engine/world.h"}

    with patch(
        "orchestrator.worktree._sibling_header_paths", side_effect=fake_helper
    ):
        asyncio.run(
            mgr.create_per_task(
                "1.1",
                sparse_paths=["src/engine/world.cpp"],
                include_headers_for_sparse=False,
            )
        )
    assert called["n"] == 0
    assert mgr.last_sparse_headers_added == 0


def test_expand_sparse_paths_capped_at_limit(tmp_path: Path) -> None:
    """When proposed additions exceed the cap, expansion is skipped."""
    import asyncio

    from orchestrator.worktree import WorktreeManager

    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "src/engine/world.cpp": "// cpp\n",
            "src/engine/world.h": "// h\n",
        },
    )
    wt_dir = tmp_path / "wts"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    over_cap = {f"src/engine/h_{i}.h" for i in range(WORKTREE_HEADER_EXPANSION_CAP + 5)}

    with patch(
        "orchestrator.worktree._sibling_header_paths",
        return_value=over_cap,
    ):
        asyncio.run(
            mgr.create_per_task(
                "1.2",
                sparse_paths=["src/engine/world.cpp"],
                include_headers_for_sparse=True,
            )
        )
    # Cap was tripped → no headers added.
    assert mgr.last_sparse_headers_added == 0
