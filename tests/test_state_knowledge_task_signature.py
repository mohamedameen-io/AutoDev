"""v0.32.0 Phase 4.3: ``compute_task_signature`` helper tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from state.knowledge import compute_task_signature


@dataclass
class _FakeTask:
    files: list[str]
    error_class: str = ""
    last_diff: str = ""


def test_signature_is_deterministic() -> None:
    a = _FakeTask(files=["src/a.py", "src/b.py"], error_class="ValueError")
    b = _FakeTask(files=["src/a.py", "src/b.py"], error_class="ValueError")
    assert compute_task_signature(a) == compute_task_signature(b)


def test_signature_independent_of_file_order() -> None:
    """Same files in different order ⇒ same signature."""
    a = _FakeTask(files=["src/a.py", "src/b.py"])
    b = _FakeTask(files=["src/b.py", "src/a.py"])
    assert compute_task_signature(a) == compute_task_signature(b)


def test_signature_changes_on_file_change() -> None:
    a = _FakeTask(files=["src/a.py"])
    b = _FakeTask(files=["src/c.py"])
    assert compute_task_signature(a) != compute_task_signature(b)


def test_signature_changes_on_error_class_change() -> None:
    a = _FakeTask(files=["src/a.py"], error_class="ValueError")
    b = _FakeTask(files=["src/a.py"], error_class="KeyError")
    assert compute_task_signature(a) != compute_task_signature(b)


def test_signature_uses_first_512_chars_of_diff() -> None:
    """Diff content beyond 512 chars must not affect the signature."""
    common_diff = "line " * 200  # ~1000 chars
    a = _FakeTask(files=["src/a.py"], last_diff=common_diff + " EXTRA-A")
    b = _FakeTask(files=["src/a.py"], last_diff=common_diff + " EXTRA-B")
    # Both first-512 slices are identical (the common prefix dominates).
    assert compute_task_signature(a) == compute_task_signature(b)


def test_signature_handles_empty_task() -> None:
    """Missing fields ⇒ stable hash (no crash)."""

    @dataclass
    class _Empty:
        pass

    sig = compute_task_signature(_Empty())
    assert isinstance(sig, str) and len(sig) == 64


def test_signature_works_on_dict_input() -> None:
    """Dict-style task descriptions are supported via ``_get_attr``."""
    task: dict[str, Any] = {
        "files": ["src/x.py"],
        "error_class": "TypeError",
        "last_diff": "diff body",
    }
    sig = compute_task_signature(task)
    assert isinstance(sig, str) and len(sig) == 64
