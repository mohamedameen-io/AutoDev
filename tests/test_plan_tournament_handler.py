"""Unit tests for :class:`src.tournament.plan_tournament.PlanContentHandler`.

Focus on the pure-function aspects (prompt rendering, hash stability, parse
logic). End-to-end flow with a ``Tournament`` lives in
:mod:`tests.test_plan_tournament_integration`.
"""

from __future__ import annotations

from tournament.plan_tournament import PlanContentHandler
from tournament.prompts import (
    ARCHITECT_B_PROMPT,
    CRITIC_PROMPT,
    SYNTHESIZER_PROMPT,
)


# ── Hash ──────────────────────────────────────────────────────────────────


def test_hash_stable() -> None:
    """Same input should produce the same hash every time."""
    h = PlanContentHandler()
    text = "# Plan: foo\n\n## Phase 1: bar\n"
    assert h.hash(text) == h.hash(text)


def test_hash_changes_on_edit() -> None:
    """A one-character change must flip the hash."""
    h = PlanContentHandler()
    a = "# Plan: foo\n"
    b = "# Plan: foo.\n"
    assert h.hash(a) != h.hash(b)


def test_hash_is_16_hex_chars() -> None:
    """The short form matches the 16-char SHA-256 prefix used across autodev."""
    h = PlanContentHandler()
    digest = h.hash("anything")
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_independent_of_handler_instance() -> None:
    """Different handler instances produce the same hash — it's pure."""
    a = PlanContentHandler().hash("xyz")
    b = PlanContentHandler().hash("xyz")
    assert a == b


# ── Rendering ─────────────────────────────────────────────────────────────


def test_render_for_critic_includes_version_a() -> None:
    """The critic prompt must embed the incumbent text verbatim."""
    h = PlanContentHandler()
    body = "UNIQUE_INCUMBENT_MARKER_123"
    rendered = h.render_for_critic(body, task_prompt="ignored-for-critic")
    assert body in rendered
    # And it should use the canonical CRITIC_PROMPT template (sanity check).
    expected = CRITIC_PROMPT.format(version_a=body)
    assert rendered == expected


def test_render_for_architect_b_includes_task_and_critique() -> None:
    h = PlanContentHandler()
    rendered = h.render_for_architect_b(
        task_prompt="TASK_MARKER_ABC",
        a="A_BODY_MARKER_XYZ",
        critic_text="CRITIC_MARKER_QRS",
    )
    assert "TASK_MARKER_ABC" in rendered
    assert "A_BODY_MARKER_XYZ" in rendered
    assert "CRITIC_MARKER_QRS" in rendered
    expected = ARCHITECT_B_PROMPT.format(
        task_prompt="TASK_MARKER_ABC",
        version_a="A_BODY_MARKER_XYZ",
        critic="CRITIC_MARKER_QRS",
    )
    assert rendered == expected


def test_render_for_synthesizer_uses_x_y_positions() -> None:
    h = PlanContentHandler()
    rendered = h.render_for_synthesizer(
        task_prompt="SYNTH_TASK_MARKER",
        x="VERSION_X_BODY",
        y="VERSION_Y_BODY",
    )
    assert "SYNTH_TASK_MARKER" in rendered
    assert "VERSION_X_BODY" in rendered
    assert "VERSION_Y_BODY" in rendered
    # Canonical template round-trip.
    expected = SYNTHESIZER_PROMPT.format(
        task_prompt="SYNTH_TASK_MARKER",
        version_x="VERSION_X_BODY",
        version_y="VERSION_Y_BODY",
    )
    assert rendered == expected


def test_render_for_judge_respects_order_map() -> None:
    """When the shuffle places AB at slot 1, PROPOSAL 1 must contain AB."""
    h = PlanContentHandler()
    order_map = {1: "AB", 2: "A", 3: "B"}
    rendered = h.render_for_judge(
        task_prompt="JUDGE_TASK",
        v_a="MARK_A_ONLY",
        v_b="MARK_B_ONLY",
        v_ab="MARK_AB_ONLY",
        order_map=order_map,
    )
    # Slot 1 should contain AB, slot 2 A, slot 3 B.
    slot1 = rendered.index("PROPOSAL 1:")
    slot2 = rendered.index("PROPOSAL 2:")
    slot3 = rendered.index("PROPOSAL 3:")
    assert rendered.index("MARK_AB_ONLY") > slot1
    assert rendered.index("MARK_AB_ONLY") < slot2
    assert rendered.index("MARK_A_ONLY") > slot2
    assert rendered.index("MARK_A_ONLY") < slot3
    assert rendered.index("MARK_B_ONLY") > slot3
    # The prompt template must still be used.
    assert "RANKING: [best], [second], [worst]" in rendered


def test_render_for_judge_populates_task_prompt() -> None:
    h = PlanContentHandler()
    rendered = h.render_for_judge(
        task_prompt="TASK_PROMPT_XYZ",
        v_a="a",
        v_b="b",
        v_ab="ab",
        order_map={1: "A", 2: "B", 3: "AB"},
    )
    assert "TASK_PROMPT_XYZ" in rendered


# ── Parsing ──────────────────────────────────────────────────────────────


def test_parse_revision_strips_whitespace() -> None:
    h = PlanContentHandler()
    assert h.parse_revision("  \n# Plan: foo\n\n", original="ignored") == (
        "# Plan: foo"
    )


def test_parse_revision_returns_non_empty() -> None:
    h = PlanContentHandler()
    # Even if original is non-empty, parse_revision trusts the LLM text.
    assert h.parse_revision("new body", original="old") == "new body"


def test_parse_synthesis_strips_whitespace() -> None:
    h = PlanContentHandler()
    assert h.parse_synthesis("\n\n# merged\n", a="a", b="b") == "# merged"


# ── Identity ──────────────────────────────────────────────────────────────


def test_render_as_markdown_identity() -> None:
    """For T=str, render_as_markdown is the identity."""
    h = PlanContentHandler()
    md = "# Plan: x\n\n## Phase 1: y\n"
    assert h.render_as_markdown(md) == md


def test_handler_conforms_to_content_handler_protocol() -> None:
    """Runtime check: PlanContentHandler satisfies ContentHandler[str]."""
    from tournament.core import ContentHandler

    h = PlanContentHandler()
    # Protocol is runtime_checkable — isinstance works.
    assert isinstance(h, ContentHandler)
