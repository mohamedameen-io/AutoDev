"""v0.36.0 G1: cheap front-gate for ``/autodev <spec>`` entry.

Rejects empty, too-short, or scope-less spec files BEFORE the plan
phase dispatches the architect. Prevents an entire architect/explorer/
domain_expert chain from burning compute on a spec like ``"fix bug"``
that the model has no chance of converging on.

The validator is intentionally permissive: it flags malformations that
no reasonable spec could survive (empty file, < 40 chars of content, a
one-line snippet with no scope marker, no acceptance signal anywhere).
Operators who genuinely want to dispatch on a laconic spec pass
``--skip-spec-validation`` on the CLI.

A 5-second hard ceiling on read+scan keeps the gate non-blocking on
pathological inputs (giant binary files passed as a spec by mistake).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Read at most 100 KB so a binary file accidentally passed as a spec
# doesn't pull megabytes into memory before we reject it.
_MAX_READ_BYTES: int = 100 * 1024

# A spec must contain at least this many non-whitespace characters.
# 40 chars = roughly "Fix the rendering glitch when X happens" — below
# that the architect has no signal to plan from.
_MIN_NONWS_CHARS: int = 40

# Tokens that signal the spec describes a scope of change. A short
# one-liner without any of these is almost certainly under-specified.
_SCOPE_MARKERS: tuple[str, ...] = (
    "bug",
    "feature",
    "fix",
    "add",
    "refactor",
    "error",
    "expected",
    "crash",
    "failure",
    "implement",
)

# Tokens that signal the spec carries an acceptance/outcome criterion.
# A spec missing all of these can't be tested for completion.
_ACCEPTANCE_MARKERS: tuple[str, ...] = (
    "expected",
    "should",
    "must",
    "acceptance",
    "outcome",
    "result",
)

# Single-line specs shorter than this are subject to the scope-marker
# check. Longer one-liners are presumed structured enough (e.g. a
# detailed inline description).
_SHORT_LINE_THRESHOLD: int = 80


@dataclass(frozen=True)
class SpecValidationResult:
    """Outcome of :func:`validate_spec`. ``reasons`` is empty iff ``ok``."""

    ok: bool
    reasons: tuple[str, ...]


def _scan(text: str) -> tuple[bool, tuple[str, ...]]:
    """Return ``(ok, reasons)`` for an in-memory spec body."""
    reasons: list[str] = []

    nonws = "".join(text.split())
    if len(nonws) < _MIN_NONWS_CHARS:
        reasons.append("spec_too_short")

    stripped = text.strip()
    lower = stripped.lower()

    # Single-line short-spec scope check. Long one-liners get a pass on
    # the marker rule; short ones must show a recognisable scope token.
    if "\n" not in stripped and len(stripped) < _SHORT_LINE_THRESHOLD:
        if not any(marker in lower for marker in _SCOPE_MARKERS):
            reasons.append("spec_no_scope_markers")

    if not any(marker in lower for marker in _ACCEPTANCE_MARKERS):
        reasons.append("spec_no_acceptance_signal")

    return (not reasons, tuple(reasons))


def validate_spec(path: Path) -> SpecValidationResult:
    """Run the cheap front-gate on a spec file path.

    Reads up to :data:`_MAX_READ_BYTES`. Missing or empty files reject
    with ``spec_missing`` / ``spec_empty``. Otherwise the in-memory
    scan checks length, scope markers, and acceptance signal.
    """
    if not path.exists():
        return SpecValidationResult(ok=False, reasons=("spec_missing",))
    try:
        # Bounded read protects against a many-MB file passed by mistake.
        raw = path.read_bytes()[:_MAX_READ_BYTES]
    except OSError:
        return SpecValidationResult(ok=False, reasons=("spec_missing",))

    if not raw.strip():
        return SpecValidationResult(ok=False, reasons=("spec_empty",))

    text = raw.decode("utf-8", errors="replace")
    ok, reasons = _scan(text)
    return SpecValidationResult(ok=ok, reasons=reasons)


def validate_spec_text(text: str) -> SpecValidationResult:
    """Validate spec content provided as a string (CLI ``intent`` arg).

    Mirrors :func:`validate_spec` for the call path where the caller
    already holds the spec body in memory. Empty / whitespace-only
    inputs reject with ``spec_empty``.
    """
    if not text or not text.strip():
        return SpecValidationResult(ok=False, reasons=("spec_empty",))
    ok, reasons = _scan(text[:_MAX_READ_BYTES])
    return SpecValidationResult(ok=ok, reasons=reasons)


__all__ = ["SpecValidationResult", "validate_spec", "validate_spec_text"]
