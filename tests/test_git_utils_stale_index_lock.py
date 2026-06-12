"""v0.40.0 (huge-repo Gap 3): clear_stale_index_lock helper.

A ``git worktree add`` killed after timing out on a huge LFS repo can
leave a stale ``.git/index.lock`` in the MAIN repo; the next ``git apply``
then fails with "Unable to create '.../index.lock': File exists". The
helper removes only a STALE + UNOWNED lock so a concurrent git op is
never disturbed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from adapters.git_utils import clear_stale_index_lock


def _make_lock(git_dir: Path, *, age_s: float = 0.0, body: str = "") -> Path:
    """Create ``<git_dir>/index.lock`` with optional age + payload body."""
    git_dir.mkdir(parents=True, exist_ok=True)
    lock = git_dir / "index.lock"
    lock.write_text(body)
    if age_s:
        old = time.time() - age_s
        os.utime(lock, (old, old))
    return lock


def test_no_lock_is_noop(tmp_path: Path) -> None:
    """No ``index.lock`` present → returns False, nothing removed."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    assert clear_stale_index_lock(git_dir) is False


def test_fresh_lock_is_kept(tmp_path: Path) -> None:
    """A just-created lock (age < max_age_s) is refused — never remove a
    lock that a real in-flight git op may hold."""
    git_dir = tmp_path / ".git"
    lock = _make_lock(git_dir, age_s=0.0)
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is False
    assert lock.exists()


def test_stale_pidless_lock_is_removed(tmp_path: Path) -> None:
    """An old lock with no parseable PID payload is cleared."""
    git_dir = tmp_path / ".git"
    lock = _make_lock(git_dir, age_s=120.0, body="")
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is True
    assert not lock.exists()


def test_stale_lock_with_dead_pid_is_removed(tmp_path: Path) -> None:
    """An old lock recording a PID that is no longer alive is cleared."""
    git_dir = tmp_path / ".git"
    # PID 2^31-1 is effectively guaranteed not to be a live process.
    dead_pid = 2_147_483_646
    lock = _make_lock(git_dir, age_s=120.0, body=f"{dead_pid}\n")
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is True
    assert not lock.exists()


def test_old_lock_with_live_owner_is_kept(tmp_path: Path) -> None:
    """Even an OLD lock is refused when its recorded PID is a LIVE process
    (a real git op holds it) — removing it would corrupt that op."""
    git_dir = tmp_path / ".git"
    live_pid = os.getpid()  # this test process is, by definition, alive
    lock = _make_lock(git_dir, age_s=120.0, body=f"{live_pid} myhost\n")
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is False
    assert lock.exists()


def test_idempotent_double_call(tmp_path: Path) -> None:
    """Calling twice on a stale lock: first removes, second is a no-op."""
    git_dir = tmp_path / ".git"
    _make_lock(git_dir, age_s=120.0, body="")
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is True
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is False


def test_pid_with_trailing_metadata_parsed(tmp_path: Path) -> None:
    """git writes ``<pid> <hostname> ...`` — the leading PID token is used.
    A dead PID with trailing metadata is still recognized and cleared."""
    git_dir = tmp_path / ".git"
    dead_pid = 2_147_483_646
    lock = _make_lock(
        git_dir, age_s=120.0, body=f"{dead_pid} build-host.local 12345\n"
    )
    assert clear_stale_index_lock(git_dir, max_age_s=30.0) is True
    assert not lock.exists()
