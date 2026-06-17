"""WS2-18 — diff-scoped ``paths`` for the nodejs/rust/go test runners.

Before this fix only the python runner (``_run_pytest``) honoured the
diff-scoped ``paths`` argument; the nodejs/rust/go runners silently dropped it
and always ran the whole suite. These tests assert the runner command is now
scoped to the changed packages/files, and that an out-of-scope file is NOT in
the selection.

ENGAGEMENT (red-on-HEAD): on the pre-fix tree, ``run_tests(..., paths=[...])``
for nodejs/rust/go emits the bare whole-suite argv (``npm test`` /
``cargo test --workspace`` / ``go test ./...``) regardless of *paths*, so every
scope assertion below fails. BROKEN-CONTROL: reverting the thread-through (drop
``paths`` in the dispatch / runners) makes the selection collapse back to the
whole suite, turning these red again.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.test_runner import run_tests


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _argv(mock_exec: AsyncMock) -> list[str]:
    """The positional argv passed to ``create_subprocess_exec``."""
    return list(mock_exec.call_args.args)


# --------------------------------------------------------------------------- #
# go: changed packages → ``go test ./pkg/...`` (in-scope), out-of-scope absent
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_go_paths_scoped_to_changed_package(tmp_path: Path) -> None:
    # A git repo (so an empty scope would mean "clean", not "no signal").
    (tmp_path / ".git").mkdir()
    proc = _make_proc(0, stdout=b"ok  pkg  0.5s")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_tests(
            tmp_path,
            language="go",
            paths=[Path("svc/api/handler.go")],
        )
    assert result.passed
    argv = _argv(mock_exec)
    assert argv[0] == "go"
    assert argv[1] == "test"
    # In-scope package present, narrowed to the changed package dir.
    assert "./svc/api/..." in argv
    # RED-on-HEAD: the dropped-paths code always emits the whole-suite "./...".
    assert "./..." not in argv, "scope must NOT collapse to the whole-suite ./..."


@pytest.mark.asyncio
async def test_go_out_of_scope_package_absent(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    proc = _make_proc(0, stdout=b"ok  pkg  0.5s")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(
            tmp_path,
            language="go",
            paths=[Path("svc/api/handler.go")],
        )
    argv = _argv(mock_exec)
    # A package NOT in the diff scope must not appear in the selection.
    assert "./svc/billing/..." not in argv
    assert not any("billing" in a for a in argv)


@pytest.mark.asyncio
async def test_go_paths_none_keeps_whole_suite(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"ok  pkg  0.5s")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(tmp_path, language="go")  # paths=None
    assert _argv(mock_exec) == ["go", "test", "./..."]


@pytest.mark.asyncio
async def test_go_empty_scope_no_git_keeps_whole_suite(tmp_path: Path) -> None:
    # No ``.git`` → empty scope means "no git signal", not "clean diff"; a
    # scoped subset would be vacuous, so fall back to the whole suite.
    proc = _make_proc(0, stdout=b"ok  pkg  0.5s")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(tmp_path, language="go", paths=[])
    assert _argv(mock_exec) == ["go", "test", "./..."]


# --------------------------------------------------------------------------- #
# rust: changed crate → ``cargo test -p <crate>`` (no ``--workspace``)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rust_paths_scoped_to_changed_crate(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    # Workspace with two member crates; only ``alpha`` is changed.
    (tmp_path / "crates" / "alpha" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "beta" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "alpha" / "Cargo.toml").write_text(
        '[package]\nname = "alpha"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "crates" / "beta" / "Cargo.toml").write_text(
        '[package]\nname = "beta"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_tests(
            tmp_path,
            language="rust",
            paths=[Path("crates/alpha/src/lib.rs")],
        )
    assert result.passed
    argv = _argv(mock_exec)
    assert argv[:2] == ["cargo", "test"]
    # In-scope crate selected via -p.
    assert "-p" in argv and "alpha" in argv
    # Out-of-scope crate absent.
    assert "beta" not in argv
    # RED-on-HEAD: dropped-paths always emits the whole ``--workspace`` suite.
    assert "--workspace" not in argv, "scope must NOT collapse to --workspace"


@pytest.mark.asyncio
async def test_rust_paths_none_keeps_workspace(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(tmp_path, language="rust")  # paths=None
    assert _argv(mock_exec) == ["cargo", "test", "--workspace"]


@pytest.mark.asyncio
async def test_rust_unresolvable_crate_falls_back_to_workspace(tmp_path: Path) -> None:
    # A virtual-workspace manifest (no [package]) → crate name unresolvable →
    # keep the whole ``--workspace`` suite rather than a vacuous empty scope.
    (tmp_path / ".git").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
    )
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(tmp_path, language="rust", paths=[Path("orphan.rs")])
    assert _argv(mock_exec) == ["cargo", "test", "--workspace"]


# --------------------------------------------------------------------------- #
# nodejs: changed test files → ``npm test -- <files>``
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_nodejs_paths_scoped_to_changed_test_file(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    # The changed test file must exist on disk to be forwarded.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.test.js").write_text("test('x', () => {})\n", encoding="utf-8")
    (tmp_path / "src" / "bar.test.js").write_text("test('y', () => {})\n", encoding="utf-8")
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_tests(
            tmp_path,
            language="nodejs",
            paths=[Path("src/foo.test.js")],
        )
    assert result.passed
    argv = _argv(mock_exec)
    assert argv[:2] == ["npm", "test"]
    # Test runner receives the changed file as a positional filter past ``--``.
    assert "--" in argv
    assert "src/foo.test.js" in argv
    # Out-of-scope test file absent.
    assert "src/bar.test.js" not in argv
    # RED-on-HEAD: dropped-paths always emits bare ``npm test`` with no ``--``.
    assert argv != ["npm", "test"], "scope must NOT collapse to bare 'npm test'"


@pytest.mark.asyncio
async def test_nodejs_paths_none_keeps_whole_suite(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(tmp_path, language="nodejs")  # paths=None
    assert _argv(mock_exec) == ["npm", "test"]


@pytest.mark.asyncio
async def test_nodejs_source_only_change_keeps_whole_suite(tmp_path: Path) -> None:
    # A non-test source change (no recognizable test file) → keep the whole
    # suite (conservative; never a vacuous empty scope).
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.js").write_text("export const x = 1\n", encoding="utf-8")
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(tmp_path, language="nodejs", paths=[Path("src/foo.js")])
    assert _argv(mock_exec) == ["npm", "test"]


@pytest.mark.asyncio
async def test_nodejs_changed_test_absent_on_disk_keeps_whole_suite(tmp_path: Path) -> None:
    # A changed test path not yet materialized in this worktree must NOT be
    # forwarded (it would make the runner error on a missing file); fall back
    # to the whole suite.
    (tmp_path / ".git").mkdir()
    proc = _make_proc(0, stdout=b"5 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_tests(
            tmp_path, language="nodejs", paths=[Path("src/ghost.test.js")]
        )
    assert _argv(mock_exec) == ["npm", "test"]
