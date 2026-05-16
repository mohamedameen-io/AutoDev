"""v0.22.4 B4: structured path normalization for architect-emitted paths.

The architect occasionally emits markdown-formatted paths (backticks,
parentheticals, trailing punctuation) that the schema validator's
narrow checks (absolute / parent-relative rejection) let through —
they then trip ``EditScopeViolation`` at execute time, wedging tasks.
v0.22.1 A4 added a minimal normalize-for-diagnostic helper that
includes both raw + normalized in the error message; B4 promotes the
normalization into a structured validation pipeline that:

* normalizes raw paths into their canonical repo-relative form,
* rejects malformed paths early with a structured
  :class:`PathValidationError` carrying ``raw``, ``reason``, and an
  optional ``suggestion``,
* feeds the architect a structured retry envelope so a second pass
  can self-correct (wired in :mod:`orchestrator.plan_phase`).

The pipeline is intentionally ordered so that earlier steps mask
later ones: NFC normalization first (so visually-identical Unicode
codepoints land in the same form), then strip-strip-strip, then
``posixpath.normpath`` for the structural cleanup, and finally the
reject phase. Each rejection raises with a specific ``reason`` so
operators see exactly what malformation tripped the validator.
"""

from __future__ import annotations

import posixpath
import unicodedata


_QUOTES: tuple[str, ...] = ("'", '"', "`")
# Trailing punctuation chars commonly present when paths are pasted
# from prose. Order matters only for repeated stripping (we strip one
# pass, not all-greedy).
_TRAILING_PUNCT: str = ".,;:)]"
_CONTROL_CHARS: frozenset[str] = frozenset(
    chr(i) for i in range(0x00, 0x20)
) | {chr(0x7F)}


class PathValidationError(Exception):
    """Architect emitted a malformed path that cannot be auto-normalized.

    Carries enough fields to be passed back to the architect for a
    structured retry. The :class:`Exception` parent's message is the
    human-readable form; ``raw``, ``reason``, ``suggestion``, and
    (v0.36.0 D1) ``error_class`` are the machine-readable surface used
    by the retry envelope.

    ``error_class`` groups failures into design-class buckets so the
    retry envelope can render one paragraph per class instead of one
    bullet per path. Default ``"missing_on_disk"`` preserves backward
    compat for sites that don't pass the kwarg.
    """

    __slots__ = ("raw", "reason", "suggestion", "error_class")

    def __init__(
        self,
        raw: str,
        reason: str,
        suggestion: str | None = None,
        error_class: str = "missing_on_disk",
    ) -> None:
        self.raw = raw
        self.reason = reason
        self.suggestion = suggestion
        self.error_class = error_class
        super().__init__(
            f"path {raw!r} rejected: {reason}"
            + (f" — try: {suggestion!r}" if suggestion else "")
        )


def normalize_path(raw: str, *, allow_glob: bool = True) -> str:
    """Return the canonical repo-relative form of *raw*, or raise.

    The pipeline (each step builds on the previous):

    1. ``unicodedata.normalize("NFC", ...)``
    2. Strip surrounding whitespace.
    3. Strip a single matched outer pair of ``'`` / ``"`` / ``` ` ``` (only
       when both ends carry the same quote char).
    4. Strip a single trailing punctuation char (``.``, ``,``, ``;``, ``:``,
       ``)``, ``]``).
    5. Strip leading ``./``.
    6. Reject if any control character is present (``\\n``, ``\\t``,
       NUL, etc.) — these are never legal in path components.
    7. Reject if empty.
    8. ``posixpath.normpath`` to collapse ``//``, ``/./``, etc.
    9. Reject absolute paths (``/`` prefix).
    10. Reject paths containing a ``..`` segment.
    11. When ``allow_glob`` is ``False`` reject the literal ``**``
        token. Cone-mode globs are otherwise allowed at this layer
        (callers gate them via ``allow_glob=False`` when the use site
        expects a literal path).
    12. Strip a trailing ``/``.

    Returns the normalized string. Raises :class:`PathValidationError`
    on any rejection.
    """
    if not isinstance(raw, str):
        raise PathValidationError(
            str(raw), reason="non_string_input", suggestion=None
        )

    s = unicodedata.normalize("NFC", raw).strip()
    if not s:
        raise PathValidationError(
            raw, reason="empty_after_strip", suggestion="omit empty paths"
        )

    # Strip a single matched outer quote pair if balanced.
    for q in _QUOTES:
        if len(s) >= 2 and s[0] == q and s[-1] == q:
            s = s[1:-1].strip()
            break

    # Strip ONE trailing punctuation char (typical sentence residue).
    if s and s[-1] in _TRAILING_PUNCT:
        s = s[:-1].rstrip()

    # Reject control characters anywhere.
    for ch in s:
        if ch in _CONTROL_CHARS:
            raise PathValidationError(
                raw,
                reason="contains_control_character",
                suggestion="paths must not contain newlines or tabs",
            )

    if s.startswith("./"):
        s = s[2:]

    if not s:
        raise PathValidationError(
            raw, reason="empty_after_strip", suggestion="omit empty paths"
        )

    # Reject ANY ``..`` segment in the pre-normpath form. We do this BEFORE
    # ``posixpath.normpath`` because normpath silently collapses
    # ``foo/../escape`` to ``escape`` — losing the suspicious traversal
    # signal. The architect emitting ``..`` is a malformation regardless
    # of whether it would semantically resolve in-tree.
    pre_parts = s.split("/")
    if any(p == ".." for p in pre_parts):
        raise PathValidationError(
            raw,
            reason="parent_segment",
            suggestion="paths must stay within the repo root",
        )

    # Structural collapse via posixpath. ``normpath`` of "src//foo" → "src/foo".
    s = posixpath.normpath(s)

    if s.startswith("/"):
        raise PathValidationError(
            raw,
            reason="absolute_path",
            suggestion=s.lstrip("/"),
        )

    if not allow_glob and "**" in s:
        raise PathValidationError(
            raw,
            reason="glob_not_allowed",
            suggestion=s.replace("**", "").strip("/"),
        )

    s = s.rstrip("/")
    if not s:
        raise PathValidationError(
            raw,
            reason="empty_after_strip",
            suggestion="omit empty paths",
        )

    return s


def validate_paths_batch(
    paths: list[str],
    *,
    allow_glob: bool = True,
) -> tuple[list[str], list[PathValidationError]]:
    """Apply :func:`normalize_path` to a batch.

    Returns a 2-tuple ``(normalized, errors)`` where ``normalized``
    contains successfully-normalized paths in input order and
    ``errors`` carries one entry per rejected path. Callers decide
    whether to fail-fast (raise on any error) or surface the
    structured errors to the architect for retry.
    """
    normalized: list[str] = []
    errors: list[PathValidationError] = []
    for raw in paths:
        try:
            normalized.append(normalize_path(raw, allow_glob=allow_glob))
        except PathValidationError as exc:
            errors.append(exc)
    return normalized, errors


__all__ = [
    "PathValidationError",
    "normalize_path",
    "validate_paths_batch",
]
