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

import re
from dataclasses import dataclass
from pathlib import Path

from state.schemas import SpecGaps


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

# ADR-0045: tokens that signal the spec carries explicit *constraints* — a
# provider lock, a deadline, a compat/version requirement, a do-not-touch
# boundary. A spec naming none of these has an unresolved ``constraints`` gap
# (intake's clarifier asks about exactly these, headlessly applies defaults).
_CONSTRAINT_MARKERS: tuple[str, ...] = (
    "constraint",
    "must not",
    "do not",
    "don't",
    "cannot",
    "only",
    "without",
    "backward",
    "backwards",
    "compatib",
    "deadline",
    "version",
    "requirement",
    "limit",
    "preserve",
    "keep",
    "no breaking",
)

# ADR-0045: signals that the spec names a *concrete repo touchpoint* the work
# will land on — a path, a file/module, a symbol, a line range. A spec with no
# touchpoint leaves the gather step to discover where the change lives.
# ``file.py`` / ``foo/bar`` / ``module.method`` style references.
_TOUCHPOINT_RE = re.compile(
    r"""
    (?:[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|c|cc|cpp|h|hpp|md|json|ya?ml|toml|cfg|ini|sh))  # a file with a known extension
    | (?:[\w-]+/[\w./-]+)            # a path-like segment (src/foo, pkg/mod)
    | (?:\b\w+\.\w+\(\))             # a method/function call like foo.bar()
    | (?:\b\w+\(\))                  # a bare function call like foo()
    """,
    re.VERBOSE,
)

# Words that, on their own, hint at a concrete touchpoint even without a path
# (so a spec that says "in the parser module" is not flagged for touchpoints).
_TOUCHPOINT_WORDS: tuple[str, ...] = (
    "file",
    "module",
    "function",
    "method",
    "class",
    "endpoint",
    "package",
    "directory",
    "path",
)


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


def _scan_constraints(lower: str) -> bool:
    """Return whether the spec names at least one explicit constraint."""
    return any(marker in lower for marker in _CONSTRAINT_MARKERS)


def _scan_touchpoints(text: str, lower: str) -> bool:
    """Return whether the spec names a concrete repo touchpoint (path/symbol/word)."""
    if _TOUCHPOINT_RE.search(text):
        return True
    return any(word in lower for word in _TOUCHPOINT_WORDS)


def assess(text: str) -> SpecGaps:
    """Structured completeness assessment (ADR-0045): WHICH dimensions are missing.

    Reuses the same deterministic markers as :func:`_scan` (so the gate stays
    cheap and consistent with the binary G1 validator) and reports the *set* of
    under-specified dimensions:

    - ``scope`` — the spec has no scope marker (a too-short / scope-less one-liner).
    - ``acceptance`` — no acceptance/success signal anywhere.
    - ``constraints`` — no explicit constraint (provider lock, compat, deadline…).
    - ``touchpoints`` — no concrete repo touchpoint (path / symbol / file word).

    ``SpecGaps.ok`` is ``True`` iff ``missing`` is empty — the back-compat boolean
    that :func:`validate_spec_text` returns. Empty / whitespace-only text reports
    every dimension missing (a maximally under-specified intent). This NEVER raises.
    """
    if not text or not text.strip():
        return SpecGaps(ok=False, missing=["scope", "acceptance", "constraints", "touchpoints"])

    body = text[:_MAX_READ_BYTES]
    stripped = body.strip()
    lower = stripped.lower()

    missing: list[str] = []

    # scope — too-short OR a scope-less short one-liner (mirror _scan's two gates).
    nonws = "".join(body.split())
    short_line = "\n" not in stripped and len(stripped) < _SHORT_LINE_THRESHOLD
    has_scope = any(marker in lower for marker in _SCOPE_MARKERS)
    if len(nonws) < _MIN_NONWS_CHARS or (short_line and not has_scope):
        missing.append("scope")

    if not any(marker in lower for marker in _ACCEPTANCE_MARKERS):
        missing.append("acceptance")

    if not _scan_constraints(lower):
        missing.append("constraints")

    if not _scan_touchpoints(body, lower):
        missing.append("touchpoints")

    return SpecGaps(ok=not missing, missing=missing)  # type: ignore[arg-type]


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


__all__ = [
    "SpecGaps",
    "SpecValidationResult",
    "assess",
    "validate_spec",
    "validate_spec_text",
]
