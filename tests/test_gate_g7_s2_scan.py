"""Gate tests for G7 (submodule + generated excludes) and S2 (diff-scoping).

G7-scan-side
------------
``run_build_check`` / ``run_syntax_check`` walk the whole tree with
``rglob``. Before this fix, that walk descended into *submodule* trees and
``.git/modules`` checkout copies, and into *generated* code (``*_pb2.py`` and
gRPC/OpenAPI stub dirs). A syntax/lint failure in an unrelated submodule or in
generated code would block an otherwise-clean fix.

The fix:

* Parse ``.gitmodules`` to learn the submodule paths and exclude them.
* Exclude ``.git`` / ``.git/modules`` checkout copies.
* Exclude known generated patterns (``*_pb2.py``, ``*_pb2_grpc.py``) and
  generated stub directories.
* A syntax error in the *host* tree is still caught (no over-exclusion).

S2 / WS2-16 (diff-scoping)
--------------------------
Both gates ``rglob`` the WHOLE tree on every task (O(repo)). The fix adds a
``paths`` parameter (repo-relative changed files). When ``paths`` is given the
scan is scoped to those files; a file *outside* ``paths`` with an error is NOT
scanned. When ``paths`` is ``None`` the whole-tree behavior is preserved
(back-compat) but with the new skip-dir exclusions applied.

Engagement-first TDD: these tests assert the *new* contract. On HEAD (before
the fix) the submodule/generated exclusion tests fail because the bad files
are scanned and block, and the ``paths`` tests fail with ``TypeError`` (no
such parameter). After the fix they go green. The broken-control test
(``test_broken_control_*``) documents that removing the exclude re-blocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.build_check import run_build_check
from qa.syntax_check import run_syntax_check


# ---------------------------------------------------------------------------
# Fixtures: a repo with a submodule + a generated *_pb2.py, both broken.
# ---------------------------------------------------------------------------


def _write_gitmodules(root: Path, sub_path: str) -> None:
    """Write a minimal ``.gitmodules`` declaring one submodule at *sub_path*."""
    (root / ".gitmodules").write_text(
        f'[submodule "{sub_path}"]\n'
        f"\tpath = {sub_path}\n"
        f"\turl = https://example.com/{sub_path}.git\n"
    )


def _make_repo_with_submodule_and_generated(root: Path) -> None:
    """Create a host tree (clean) + a broken submodule + a broken generated file."""
    # Host tree: clean.
    (root / "app.py").write_text("def host():\n    return 1\n")

    # Submodule: declared in .gitmodules, contains a SYNTAX ERROR.
    sub = root / "vendor" / "thirdparty"
    sub.mkdir(parents=True)
    (sub / "broken.py").write_text("def broken(\n")  # unterminated def
    _write_gitmodules(root, "vendor/thirdparty")

    # Generated protobuf file in the host tree: contains a SYNTAX ERROR but is
    # generated, so a real diff would never touch it and it must not block.
    (root / "messages_pb2.py").write_text("def gen(\n")  # broken generated code
    (root / "service_pb2_grpc.py").write_text("class Stub(\n")  # broken grpc stub


# ---------------------------------------------------------------------------
# G7 — build_check excludes submodule + generated; still catches host errors.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_check_excludes_submodule_and_generated(tmp_path: Path) -> None:
    """A broken submodule/generated file does NOT block build_check."""
    _make_repo_with_submodule_and_generated(tmp_path)
    result = await run_build_check(tmp_path, language="python")
    assert result.passed, f"submodule/generated error wrongly blocked: {result.details}"


@pytest.mark.asyncio
async def test_build_check_still_catches_host_syntax_error(tmp_path: Path) -> None:
    """A syntax error in the HOST tree is still caught after the exclusions."""
    _make_repo_with_submodule_and_generated(tmp_path)
    # Introduce a host-tree error in a non-generated, non-submodule file.
    (tmp_path / "real_bug.py").write_text("def host_bug(\n")
    result = await run_build_check(tmp_path, language="python")
    assert not result.passed, "host-tree syntax error must still block"


# ---------------------------------------------------------------------------
# G7 — syntax_check excludes submodule + generated; still catches host errors.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_excludes_submodule_and_generated(tmp_path: Path) -> None:
    """A broken submodule/generated file does NOT block syntax_check."""
    _make_repo_with_submodule_and_generated(tmp_path)
    result = await run_syntax_check(tmp_path, language="python")
    assert result.passed, f"submodule/generated error wrongly blocked: {result.details}"


@pytest.mark.asyncio
async def test_syntax_check_still_catches_host_syntax_error(tmp_path: Path) -> None:
    """A syntax error in the HOST tree is still caught after the exclusions."""
    _make_repo_with_submodule_and_generated(tmp_path)
    (tmp_path / "real_bug.py").write_text("def host_bug(\n")
    result = await run_syntax_check(tmp_path, language="python")
    assert not result.passed, "host-tree syntax error must still block"


# ---------------------------------------------------------------------------
# G7 — .git/modules checkout copies are excluded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_excludes_git_modules_tree(tmp_path: Path) -> None:
    """A broken .py inside .git/modules must not be scanned."""
    (tmp_path / "app.py").write_text("x = 1\n")
    gitmod = tmp_path / ".git" / "modules" / "sub" / "src"
    gitmod.mkdir(parents=True)
    (gitmod / "broken.py").write_text("def broken(\n")
    result = await run_syntax_check(tmp_path, language="python")
    assert result.passed, f".git/modules error wrongly blocked: {result.details}"


@pytest.mark.asyncio
async def test_build_check_excludes_git_modules_tree(tmp_path: Path) -> None:
    """A broken .py inside .git/modules must not block build_check."""
    (tmp_path / "app.py").write_text("x = 1\n")
    gitmod = tmp_path / ".git" / "modules" / "sub" / "src"
    gitmod.mkdir(parents=True)
    (gitmod / "broken.py").write_text("def broken(\n")
    result = await run_build_check(tmp_path, language="python")
    assert result.passed, f".git/modules error wrongly blocked: {result.details}"


# ---------------------------------------------------------------------------
# S2 / WS2-16 — diff-scoping via the ``paths`` parameter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_accepts_paths_and_scopes(tmp_path: Path) -> None:
    """A file OUTSIDE ``paths`` with an error is NOT scanned when paths is given."""
    (tmp_path / "changed.py").write_text("def changed():\n    return 1\n")
    (tmp_path / "untouched.py").write_text("def untouched(\n")  # broken, not in scope
    result = await run_syntax_check(
        tmp_path, language="python", paths=[Path("changed.py")]
    )
    assert result.passed, f"out-of-scope error wrongly blocked: {result.details}"


@pytest.mark.asyncio
async def test_build_check_accepts_paths_and_scopes(tmp_path: Path) -> None:
    """A file OUTSIDE ``paths`` with an error is NOT scanned when paths is given."""
    (tmp_path / "changed.py").write_text("def changed():\n    return 1\n")
    (tmp_path / "untouched.py").write_text("def untouched(\n")  # broken, not in scope
    result = await run_build_check(
        tmp_path, language="python", paths=[Path("changed.py")]
    )
    assert result.passed, f"out-of-scope error wrongly blocked: {result.details}"


@pytest.mark.asyncio
async def test_syntax_check_paths_catches_in_scope_error(tmp_path: Path) -> None:
    """When the IN-scope file has an error, the gate still blocks."""
    (tmp_path / "changed.py").write_text("def changed(\n")  # broken, in scope
    (tmp_path / "untouched.py").write_text("x = 1\n")
    result = await run_syntax_check(
        tmp_path, language="python", paths=[Path("changed.py")]
    )
    assert not result.passed, "in-scope error must block"


@pytest.mark.asyncio
async def test_build_check_paths_catches_in_scope_error(tmp_path: Path) -> None:
    """When the IN-scope file has an error, the gate still blocks."""
    (tmp_path / "changed.py").write_text("def changed(\n")  # broken, in scope
    (tmp_path / "untouched.py").write_text("x = 1\n")
    result = await run_build_check(
        tmp_path, language="python", paths=[Path("changed.py")]
    )
    assert not result.passed, "in-scope error must block"


@pytest.mark.asyncio
async def test_syntax_check_paths_none_is_back_compat(tmp_path: Path) -> None:
    """``paths=None`` (default) preserves whole-tree behavior."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("def broken(\n")  # anywhere in tree
    result = await run_syntax_check(tmp_path, language="python")
    assert not result.passed, "whole-tree walk must still find the error"


@pytest.mark.asyncio
async def test_build_check_paths_none_is_back_compat(tmp_path: Path) -> None:
    """``paths=None`` (default) preserves whole-tree behavior."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("def broken(\n")  # anywhere in tree
    result = await run_build_check(tmp_path, language="python")
    assert not result.passed, "whole-tree walk must still find the error"


@pytest.mark.asyncio
async def test_paths_excludes_generated_even_when_listed(tmp_path: Path) -> None:
    """Generated files are skipped even when explicitly in ``paths``.

    A broken ``*_pb2.py`` that happens to appear in the diff (e.g. a regenerated
    stub) must not block the unrelated fix.
    """
    (tmp_path / "changed.py").write_text("x = 1\n")
    (tmp_path / "regen_pb2.py").write_text("def gen(\n")  # broken generated
    result = await run_syntax_check(
        tmp_path,
        language="python",
        paths=[Path("changed.py"), Path("regen_pb2.py")],
    )
    assert result.passed, f"generated file in paths wrongly blocked: {result.details}"


# ---------------------------------------------------------------------------
# BROKEN-CONTROL — proves the exclusion is load-bearing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_without_exclude_submodule_reblocks(tmp_path: Path) -> None:
    """Sanity: if the submodule guard were absent, the broken submodule blocks.

    This documents the value of the exclusion. We simulate "no exclude" by
    putting the same broken file in an ORDINARY (non-submodule, non-generated)
    directory — the gate MUST still catch it. If this test ever passes-green
    while the real submodule test also passes, the exclusion is over-broad.
    """
    (tmp_path / "app.py").write_text("x = 1\n")
    # An ordinary subdir that is NOT a submodule and NOT generated.
    ordinary = tmp_path / "vendor" / "thirdparty"
    ordinary.mkdir(parents=True)
    (ordinary / "broken.py").write_text("def broken(\n")
    # No .gitmodules → "vendor/thirdparty" is just a normal dir → must block.
    result = await run_syntax_check(tmp_path, language="python")
    assert not result.passed, (
        "without a .gitmodules declaration, vendor/thirdparty is an ordinary "
        "dir and its error MUST block (proves the exclude is gitmodules-driven, "
        "not a blanket 'vendor' skip)"
    )
