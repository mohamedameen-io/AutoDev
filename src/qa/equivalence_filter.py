"""Static equivalence filter for surviving mutants (v0.19.0 — Stage 1).

A mutmut survivor that is **textually equivalent** to the original after
normalization (whitespace, comments, semantically-redundant rewrites) is
not a genuine test sufficiency gap — it's a no-op mutation. Stage 1
catches these via Python AST normalization + bytecode hash. Stage 2
(LLM judge) handles the harder semantic-equivalence cases.

API surface is intentionally small:

  * :func:`is_static_equivalent` — boolean equivalence verdict for a
    pair of source strings.
  * :func:`normalize_python_source` — return a canonical AST dump for
    the source. Useful for keying caches.

Non-Python sources fall back to whitespace+comment-stripped textual
comparison. Python's AST gives us a stronger signal: ``a + b`` and
``a +  b`` (extra space) compile to identical AST → equivalent. ``a + 0``
and ``a`` compile to *different* AST → caller will not declare equivalent
(though they ARE semantically equivalent — that's Stage 2's job).
"""

from __future__ import annotations

import ast
import hashlib
import re


def _strip_comments_and_whitespace(source: str) -> str:
    """Drop ``#`` line comments and collapse whitespace runs.

    Used as the non-Python fallback. Cheap and good-enough for catching
    pure formatting mutations on JS/TS/C++ sources.
    """
    # Remove block-style /* ... */ comments (C-family).
    out = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    # Remove line comments (Python: #, C-family: //).
    out = re.sub(r"//[^\n]*", "", out)
    out = re.sub(r"#[^\n]*", "", out)
    # Collapse whitespace.
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def normalize_python_source(source: str) -> str | None:
    """Return a canonical AST dump for *source*, or None on parse error.

    The dump is annotation-stripped to ignore line/column numbers and
    type-comment fields — only the structural AST contributes to the
    canonical form.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def _ast_hash(source: str) -> str | None:
    """SHA-256 of canonical AST dump, or None on parse error."""
    canonical = normalize_python_source(source)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_static_equivalent(original_code: str, mutant_code: str) -> bool:
    """Decide whether *original_code* and *mutant_code* are statically equivalent.

    Equivalence rules:

      1. Identical text → equivalent.
      2. Both parse as Python AND share the same canonical AST dump →
         equivalent.
      3. Whitespace+comment-stripped equality (non-Python fallback) →
         equivalent.
      4. Otherwise → not equivalent (Stage 2 may still rule them
         semantically equivalent).
    """
    if original_code == mutant_code:
        return True

    h_orig = _ast_hash(original_code)
    h_mut = _ast_hash(mutant_code)
    if h_orig is not None and h_mut is not None:
        return h_orig == h_mut

    # Non-Python (or unparseable) fallback: textual normalization.
    return _strip_comments_and_whitespace(original_code) == _strip_comments_and_whitespace(
        mutant_code
    )


__all__ = [
    "is_static_equivalent",
    "normalize_python_source",
]
