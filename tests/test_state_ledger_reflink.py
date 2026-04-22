"""Focused tests for reflink-aware ledger append behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import state.ledger as ledger_mod
from state.ledger import _atomic_append, _clone_file


def test_atomic_append_fallback_path_appends_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When clone support is unavailable, append falls back to byte-copy."""
    lp = tmp_path / "ledger.jsonl"
    lp.write_text('{"seq":1}\n', encoding="utf-8")

    monkeypatch.setattr(ledger_mod, "_clone_file", lambda _s, _d: False)
    _atomic_append(lp, '{"seq":2}\n')

    assert lp.read_text(encoding="utf-8") == '{"seq":1}\n{"seq":2}\n'


def test_clone_file_does_not_mutate_source_when_supported(tmp_path: Path) -> None:
    """Reflink clone + writes to clone must not alter source bytes."""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_bytes(b"alpha\n")

    if not _clone_file(src, dst):
        pytest.skip("filesystem/runtime does not support clonefile/FICLONE")

    with dst.open("ab") as fh:
        fh.write(b"beta\n")
        fh.flush()
        os.fsync(fh.fileno())

    assert src.read_bytes() == b"alpha\n"
    assert dst.read_bytes() == b"alpha\nbeta\n"


def test_atomic_append_failure_leaves_live_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after tmp write but before replace keeps live ledger unchanged."""
    lp = tmp_path / "ledger.jsonl"
    original = '{"seq":1}\n'
    lp.write_text(original, encoding="utf-8")

    def _boom(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(ledger_mod.os, "fsync", _boom)

    with pytest.raises(OSError, match="simulated fsync failure"):
        _atomic_append(lp, '{"seq":2}\n')

    assert lp.read_text(encoding="utf-8") == original


def test_clone_file_returns_false_for_missing_source(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    dst = tmp_path / "dst.txt"
    assert _clone_file(missing, dst) is False

