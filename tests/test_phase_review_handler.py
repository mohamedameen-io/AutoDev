"""Tests for :class:`tournament.phase_review._PhaseReviewContentHandler`.

One test per ContentHandler protocol method; mirrors the shape of
``tests/test_impl_tournament_handler.py``.
"""

from __future__ import annotations

from state.schemas import AcceptanceCriterion
from tournament.phase_review import (
    PhaseReviewBundle,
    _PhaseReviewContentHandler,
)


def _make_bundle(
    variant_label: str = "A",
    direction_text: str = "",
) -> PhaseReviewBundle:
    return PhaseReviewBundle(
        phase_id="1",
        phase_title="Investigate the dispatcher hang",
        baseline_commit="aaaa1111",
        tip_commit="bbbb2222",
        diff=(
            "diff --git a/worker.py b/worker.py\n"
            "+++ b/worker.py\n"
            "@@ -1 +1,2 @@\n"
            "+# refactored worker\n"
        ),
        files_changed=["worker.py", "queue.py"],
        acceptance=[
            AcceptanceCriterion(
                id="ph-ac-1", description="dispatcher tests no longer flake"
            ),
            AcceptanceCriterion(
                id="ph-ac-2", description="root cause documented", met=True
            ),
        ],
        task_summary="3 of 3 tasks complete",
        test_summary="passed=42 failed=0 total=42",
        variant_label=variant_label,  # type: ignore[arg-type]
        direction_text=direction_text,
    )


# ---------------------------------------------------------------------------
# render_for_critic
# ---------------------------------------------------------------------------


def test_render_for_critic_includes_diff_and_acceptance() -> None:
    h = _PhaseReviewContentHandler()
    t = _make_bundle()
    out = h.render_for_critic(t, "Refactor the dispatcher.")
    # Phase identification surfaces.
    assert "Phase: 1 — Investigate the dispatcher hang" in out or (
        "PHASE: 1" in out and "Investigate the dispatcher hang" in out
    )
    # Acceptance items appear with checkmark indicators.
    assert "dispatcher tests no longer flake" in out
    assert "[x]" in out and "root cause documented" in out
    # The diff is included.
    assert "refactored worker" in out
    # The original task prompt is threaded through.
    assert "Refactor the dispatcher." in out


# ---------------------------------------------------------------------------
# render_for_architect_b
# ---------------------------------------------------------------------------


def test_render_for_architect_b_uses_critic_text() -> None:
    h = _PhaseReviewContentHandler()
    t = _make_bundle()
    critic = "- The dispatcher tests still flake on macOS."
    out = h.render_for_architect_b(
        "Refactor the dispatcher.", t, critic
    )
    assert critic in out
    # The architect_b prompt instructs corrective bullet output format.
    assert "CORRECTIVE DIRECTION" in out or "bullet" in out


# ---------------------------------------------------------------------------
# render_for_synthesizer
# ---------------------------------------------------------------------------


def test_render_for_synthesizer_combines_a_and_b_directions() -> None:
    h = _PhaseReviewContentHandler()
    a = _make_bundle("A")
    b = _make_bundle("B", direction_text="- Fix flake on macOS")
    out = h.render_for_synthesizer("Refactor the dispatcher.", a, b)
    # B's direction text is visible in the synthesis prompt.
    assert "Fix flake on macOS" in out
    # A's diff is also visible (one of the two inputs).
    assert "refactored worker" in out


# ---------------------------------------------------------------------------
# render_for_judge
# ---------------------------------------------------------------------------


def test_render_for_judge_includes_three_proposals_in_order_map() -> None:
    h = _PhaseReviewContentHandler()
    a = _make_bundle("A")
    b = _make_bundle("B", direction_text="- Direction B")
    ab = _make_bundle("AB", direction_text="- Direction AB")
    order = {1: "B", 2: "AB", 3: "A"}
    out = h.render_for_judge("Refactor the dispatcher.", a, b, ab, order)
    # All three proposal blocks are present.
    assert "PROPOSAL 1:" in out
    assert "PROPOSAL 2:" in out
    assert "PROPOSAL 3:" in out
    # B (slot 1) and AB (slot 2) appear before A (slot 3) so the judge
    # cannot infer position from canonical label.
    pos_b = out.index("Direction B")
    pos_ab = out.index("Direction AB")
    pos_a = out.index("refactored worker")
    assert pos_b < pos_ab < pos_a
    # Mandatory length penalty directive is present.
    assert "LENGTH PENALTY" in out


# ---------------------------------------------------------------------------
# parse_revision
# ---------------------------------------------------------------------------


def test_parse_revision_returns_bundle_with_direction_text() -> None:
    h = _PhaseReviewContentHandler()
    a = _make_bundle("A")
    parsed = h.parse_revision("- corrective bullet 1\n- corrective bullet 2", a)
    assert parsed.variant_label == "B"
    assert "corrective bullet 1" in parsed.direction_text
    assert "corrective bullet 2" in parsed.direction_text
    # The other fields are preserved (so the bundle stays interpretable).
    assert parsed.phase_id == a.phase_id
    assert parsed.diff == a.diff


# ---------------------------------------------------------------------------
# parse_synthesis
# ---------------------------------------------------------------------------


def test_parse_synthesis_returns_bundle_with_direction_text() -> None:
    h = _PhaseReviewContentHandler()
    a = _make_bundle("A")
    b = _make_bundle("B", direction_text="- B direction")
    parsed = h.parse_synthesis("- merged correction", a, b)
    assert parsed.variant_label == "AB"
    assert "merged correction" in parsed.direction_text


# ---------------------------------------------------------------------------
# render_as_markdown
# ---------------------------------------------------------------------------


def test_render_as_markdown() -> None:
    h = _PhaseReviewContentHandler()
    t = _make_bundle("AB", direction_text="- merged direction")
    md = h.render_as_markdown(t)
    assert "# PhaseReviewBundle AB" in md
    assert "phase_id: 1" in md
    assert "merged direction" in md
    assert "## Diff" in md


# ---------------------------------------------------------------------------
# hash
# ---------------------------------------------------------------------------


def test_hash_stable_across_calls() -> None:
    h = _PhaseReviewContentHandler()
    t = _make_bundle("A")
    assert h.hash(t) == h.hash(t)
    # Variant label participates → A vs B differ.
    b = _make_bundle("B", direction_text="- B direction")
    assert h.hash(t) != h.hash(b)
