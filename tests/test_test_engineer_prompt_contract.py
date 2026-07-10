"""WS1 (1e) static contract pins for the ``test_engineer`` role prompt.

The prompt was ported verbatim from a different framework and instructed the
model to use a ``test_runner`` tool that does NOT exist for either adapter this
repo supports, while forbidding the one tool actually granted (Bash). Its
documented output contract also did not match what ``_parse_test_counts``
actually parses. These pins prevent that drift from recurring.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.execute_phase import _parse_test_counts

_PROMPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agents"
    / "prompts"
    / "test_engineer.md"
)


def _read() -> str:
    return _PROMPT.read_text(encoding="utf-8")


def test_prompt_no_longer_references_nonexistent_test_runner_tool() -> None:
    """The nonexistent ``test_runner`` tool must be gone entirely."""
    text = _read()
    assert "test_runner" not in text, (
        "test_engineer.md still references the nonexistent 'test_runner' tool."
    )


def test_prompt_gives_direct_bash_guidance() -> None:
    """The prohibition on direct shell runners is replaced by Bash guidance —
    Bash is the one tool actually granted to test_engineer."""
    text = _read()
    assert "Bash" in text, "test_engineer.md should instruct the agent to use Bash."
    assert "NO direct shell runners" not in text, (
        "test_engineer.md still forbids the direct shell runner it must use."
    )


def test_prompt_output_format_is_parseable_by_parser() -> None:
    """The prompt's documented output format must be exactly what
    ``_parse_test_counts`` parses. Running the parser over the prompt body must
    extract a non-zero total from the concrete example it documents."""
    text = _read()
    passed, failed, total = _parse_test_counts(text)
    assert total > 0, (
        "test_engineer.md does not document a 'passed=N failed=M total=T' "
        "example that _parse_test_counts can extract."
    )
    # Sanity: the documented example is internally consistent.
    assert passed + failed <= total


def test_prompt_preserves_autonomy_clause() -> None:
    """The shared autonomy clause (enforced fleet-wide by test_prompts_lint)
    must survive the rewrite."""
    text = _read()
    assert "_autonomy_clause.md — keep in sync" in text
    assert "ESCALATE:" in text
    assert "running unattended" in text
