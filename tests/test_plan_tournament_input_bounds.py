"""Input-size bounds for :class:`tournament.plan_tournament.PlanContentHandler`.

v0.42.1 (F2a / A4 gate-c): the plan tournament used to inline the FULL plan
markdown into its critic / architect_b / synthesizer / judge prompts with no
size bound. In the field (Run-5) that produced 190K-262K-token reads and the
``critic_t error_max_turns`` exhaustion. The sibling tournaments
(:mod:`tournament.phase_review`, :mod:`tournament.impl_tournament`) already
bound their inputs with the shared :func:`tournament.util._limit` helper; these
tests pin the SAME caps onto the plan tournament's render functions.

Each test feeds an oversized plan string and asserts (a) the truncation marker
is present and (b) the oversized body is NOT embedded in full.
"""

from __future__ import annotations

from tournament.plan_tournament import PlanContentHandler


# An oversized incumbent/version plan — well past every cap (12000 / 8000).
_OVERSIZE = "X" * 50000

# The shared marker emitted by ``tournament.util._limit`` on truncation.
_MARKER = "... (truncated"


def test_render_for_critic_bounds_incumbent() -> None:
    """The critic prompt must truncate an oversized incumbent (cap 12000)."""
    h = PlanContentHandler()
    rendered = h.render_for_critic(_OVERSIZE, task_prompt="ignored-for-critic")
    assert _MARKER in rendered
    # The full 50000-char body must NOT survive; nor may a 12001-char run of
    # the body (i.e. it is capped at <= 12000 chars of the original).
    assert _OVERSIZE not in rendered
    assert "X" * 12001 not in rendered


def test_render_for_architect_b_bounds_incumbent_and_critique() -> None:
    """architect_b must truncate both the incumbent (12000) and critique (12000)."""
    h = PlanContentHandler()
    big_critique = "C" * 50000
    rendered = h.render_for_architect_b(
        task_prompt="short task",
        a=_OVERSIZE,
        critic_text=big_critique,
    )
    assert _MARKER in rendered
    assert _OVERSIZE not in rendered
    assert "X" * 12001 not in rendered
    # The large LLM critique is bounded too.
    assert big_critique not in rendered
    assert "C" * 12001 not in rendered


def test_render_for_synthesizer_bounds_both_versions() -> None:
    """The synthesizer must truncate BOTH versions (cap 8000 each)."""
    h = PlanContentHandler()
    x = "X" * 50000
    y = "Y" * 50000
    rendered = h.render_for_synthesizer(task_prompt="short task", x=x, y=y)
    assert _MARKER in rendered
    assert x not in rendered
    assert y not in rendered
    assert "X" * 8001 not in rendered
    assert "Y" * 8001 not in rendered


def test_render_for_judge_bounds_each_version() -> None:
    """The judge prompt must truncate EACH of the 3 plan versions (cap 8000)."""
    h = PlanContentHandler()
    v_a = "A" * 50000
    v_b = "B" * 50000
    v_ab = "M" * 50000
    rendered = h.render_for_judge(
        task_prompt="short task",
        v_a=v_a,
        v_b=v_b,
        v_ab=v_ab,
        order_map={1: "A", 2: "B", 3: "AB"},
    )
    assert _MARKER in rendered
    assert v_a not in rendered
    assert v_b not in rendered
    assert v_ab not in rendered
    assert "A" * 8001 not in rendered
    assert "B" * 8001 not in rendered
    assert "M" * 8001 not in rendered
