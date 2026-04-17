"""Tests for :class:`ImplContentHandler` rendering and parsing."""

from __future__ import annotations

from tournament import ImplBundle, ImplContentHandler


def _bundle(
    *,
    task_id: str = "1.1",
    task_description: str = "Add foo()",
    diff: str = "+def foo(): pass",
    files_changed: list[str] | None = None,
    tests_passed: int = 3,
    tests_failed: int = 0,
    tests_total: int = 3,
    test_output_excerpt: str = "3 passed",
    variant_label: str = "A",
    notes: str = "",
) -> ImplBundle:
    return ImplBundle(
        task_id=task_id,
        task_description=task_description,
        diff=diff,
        files_changed=files_changed or ["foo.py"],
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        tests_total=tests_total,
        test_output_excerpt=test_output_excerpt,
        variant_label=variant_label,  # type: ignore[arg-type]
        notes=notes,
    )


def test_render_for_critic_contains_task_and_diff() -> None:
    handler = ImplContentHandler()
    b = _bundle()
    text = handler.render_for_critic(b, "Implement foo()")
    assert "Implement foo()" in text
    assert "+def foo(): pass" in text
    assert "passed=3" in text
    assert "failed=0" in text


def test_render_for_architect_b_contains_critic_text() -> None:
    handler = ImplContentHandler()
    b = _bundle()
    text = handler.render_for_architect_b(
        "Implement foo()", b, "Critic: missing docstring"
    )
    assert "Critic: missing docstring" in text
    assert "+def foo(): pass" in text


def test_render_for_synthesizer_contains_both_versions() -> None:
    handler = ImplContentHandler()
    x = _bundle(diff="+def foo(): return 1", variant_label="A")
    y = _bundle(diff="+def foo(): return 2", variant_label="B")
    text = handler.render_for_synthesizer("Implement foo()", x, y)
    assert "+def foo(): return 1" in text
    assert "+def foo(): return 2" in text


def test_render_for_judge_contains_three_proposals() -> None:
    handler = ImplContentHandler()
    v_a = _bundle(diff="+def foo(): return 1", variant_label="A")
    v_b = _bundle(diff="+def foo(): return 2", variant_label="B")
    v_ab = _bundle(diff="+def foo(): return 3", variant_label="AB")
    order_map = {1: "A", 2: "B", 3: "AB"}
    text = handler.render_for_judge("Implement foo()", v_a, v_b, v_ab, order_map)
    assert "PROPOSAL 1:" in text
    assert "PROPOSAL 2:" in text
    assert "PROPOSAL 3:" in text


def test_parse_revision_returns_placeholder_bundle() -> None:
    handler = ImplContentHandler()
    original = _bundle()
    result = handler.parse_revision("- Fix the edge case\n- Add docstring", original)
    assert result.task_id == original.task_id
    assert result.variant_label == "B"
    assert "Fix the edge case" in result.notes
    assert result.diff == ""


def test_parse_synthesis_returns_placeholder_ab_bundle() -> None:
    handler = ImplContentHandler()
    a = _bundle(variant_label="A")
    b = _bundle(variant_label="B")
    result = handler.parse_synthesis("- Combine both approaches", a, b)
    assert result.task_id == a.task_id
    assert result.variant_label == "AB"
    assert "Combine both approaches" in result.notes


def test_render_as_markdown_contains_key_fields() -> None:
    handler = ImplContentHandler()
    b = _bundle(diff="+def foo(): pass", notes="some notes")
    md = handler.render_as_markdown(b)
    assert "# ImplBundle" in md
    assert "task_id: 1.1" in md
    assert "+def foo(): pass" in md
    assert "some notes" in md


def test_hash_differs_for_different_diffs() -> None:
    handler = ImplContentHandler()
    a = _bundle(diff="+def foo(): return 1")
    b = _bundle(diff="+def foo(): return 2")
    assert handler.hash(a) != handler.hash(b)


def test_hash_differs_for_different_variant_labels() -> None:
    handler = ImplContentHandler()
    a = _bundle(diff="+def foo(): pass", variant_label="A")
    b = _bundle(diff="+def foo(): pass", variant_label="B")
    assert handler.hash(a) != handler.hash(b)


def test_hash_stable_for_same_bundle() -> None:
    handler = ImplContentHandler()
    b = _bundle()
    assert handler.hash(b) == handler.hash(b)


def test_render_for_critic_truncates_long_diff() -> None:
    handler = ImplContentHandler()
    long_diff = "+" + "x" * 15000
    b = _bundle(diff=long_diff)
    text = handler.render_for_critic(b, "task")
    # Should be truncated — the full diff is 15001 chars but limit is 12000.
    assert "truncated" in text
