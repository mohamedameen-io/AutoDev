"""WS-5 static contract pins for the acceptance-oracle discipline.

Forensic finding (sympy-11400): the PLAN phase derived a bug-fix task's
acceptance criteria from the issue's *own* example output, so a BUGGY example
(``Ne(x, 0)``) became the acceptance oracle baked into ``plan.json``. The
plan-critic could not empirically refute it because it was Read-only /
Bash-denied.

Two prompt-level defenses are pinned here:

  1. ``architect.md`` (the plan-DRAFTING architect) must instruct that a
     bug-fix task's acceptance oracle be grounded in an *executed reproduction*
     (a red test failing on the pre-fix tree), NOT the issue's example output
     copied verbatim as ground truth.
  2. ``tournament.prompts.ARCHITECT_B_PROMPT`` (the plan-critic / reviser, which
     WS-5 grants Bash) must direct architect_b to USE that new capability to
     validate a suspect bug-fix oracle against a reproduction before trusting
     the issue's example.

These are static pins (mirror ``test_test_engineer_prompt_contract.py``): they
assert the discipline text is present, not the model's runtime behavior.
"""

from __future__ import annotations

from pathlib import Path

_ARCHITECT_PROMPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agents"
    / "prompts"
    / "architect.md"
)


def _read_architect() -> str:
    return _ARCHITECT_PROMPT.read_text(encoding="utf-8")


def test_architect_prompt_grounds_oracle_in_executed_reproduction() -> None:
    """architect.md must require a bug-fix acceptance oracle to be grounded in
    an executed reproduction (red test failing on the pre-fix tree)."""
    text = _read_architect()
    assert "ACCEPTANCE ORACLE DISCIPLINE" in text, (
        "architect.md is missing the acceptance-oracle discipline section."
    )
    assert "executed reproduction" in text, (
        "architect.md must require the acceptance oracle to be grounded in an "
        "executed reproduction, not the issue's example."
    )
    assert "pre-fix tree" in text, (
        "architect.md must frame the oracle as a red test that fails on the "
        "pre-fix tree."
    )


def test_architect_prompt_forbids_copying_issue_example_as_ground_truth() -> None:
    """architect.md must explicitly warn against copying the issue's example
    output verbatim as ground truth."""
    text = _read_architect()
    assert "verbatim" in text, (
        "architect.md must warn against copying the issue's example output "
        "verbatim as the oracle."
    )
    assert "ground truth" in text, (
        "architect.md must state the issue's example is not ground truth."
    )


def test_architect_b_prompt_directs_empirical_oracle_validation() -> None:
    """The plan-critic prompt (architect_b) must direct it to USE its WS-5 tool
    grant to validate a suspect bug-fix oracle against a reproduction rather
    than trusting the issue's example."""
    from tournament import prompts as tp

    body = tp.ARCHITECT_B_PROMPT
    assert "reproduction" in body, (
        "ARCHITECT_B_PROMPT must direct architect_b to run a reproduction to "
        "validate a bug-fix acceptance oracle."
    )
    assert "Bash" in body, (
        "ARCHITECT_B_PROMPT must reference the Bash capability WS-5 grants it."
    )
    assert "verbatim" in body, (
        "ARCHITECT_B_PROMPT must warn against trusting the issue's example "
        "copied verbatim."
    )
