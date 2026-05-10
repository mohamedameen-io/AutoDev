"""Per-language symbol extractors for the file/symbol index (v0.25.0).

The file/symbol index in :mod:`state.file_index` builds a sqlite-FTS5 store
of every tracked source file and the top-level symbols within it
(functions, classes, methods, namespaces, structs). The index uses these
symbols to feed the architect a CANDIDATE_FILES digest at planning time so
it stops inventing file paths.

Each extractor follows a thin :class:`LanguageExtractor` Protocol:

  * ``extensions`` — the set of lowercase suffixes (with leading dot) the
    extractor handles. The dispatcher routes by suffix.
  * ``lang_tag`` — the short label stored in ``files.lang`` for downstream
    queries (e.g. ``"py"``, ``"cpp"``, ``"ts"``, ``"other"``).
  * ``extract(source: str)`` — return a list of :class:`ExtractedSymbol`
    dicts. ``signature`` is at most ~120 chars (the first declaration line
    truncated). Best-effort: malformed source returns ``[]`` rather than
    raising.

Extractors mirror the v0.19.0 pattern at ``qa/cpp_symbols.py:32-42``:
prefer tree-sitter when the optional native binding is installed, fall
back to regex / :mod:`ast` otherwise. The regex fallback is intentionally
coarser; the index's job is to be a candidate generator, not a compiler.
"""

from __future__ import annotations

from typing import Protocol, TypedDict


class ExtractedSymbol(TypedDict):
    """One symbol extracted from a source file.

    Attributes:
        name: The bare identifier (e.g. ``"parse_plan_markdown"``).
        kind: One of ``function|class|method|namespace|struct|enum|var``.
            Extractors that cannot distinguish ``method`` from ``function``
            return ``"function"`` (fewer false hits in FTS).
        line: 1-based line number of the declaration.
        col: 0-based column. ``0`` when the extractor cannot resolve it.
        signature: At most ~120 chars of the declaration line, trimmed.
            Used for human-readable rendering in the candidate digest.
    """

    name: str
    kind: str
    line: int
    col: int
    signature: str


class LanguageExtractor(Protocol):
    """Per-language symbol extractor protocol.

    See module docstring for the full contract. The dispatcher in
    :mod:`state.file_index` walks :data:`EXTRACTORS` in order; the first
    extractor whose ``extensions`` set contains the file's suffix wins.
    """

    extensions: frozenset[str]
    lang_tag: str

    def extract(self, source: str) -> list[ExtractedSymbol]: ...


# Concrete extractors are imported lazily inside the function below so that
# ``ExtractedSymbol`` and ``LanguageExtractor`` are fully defined when the
# extractor modules pull them in at their own import time.
EXTRACTORS: list[LanguageExtractor] = []


def _populate_extractors() -> None:
    """Lazy-build :data:`EXTRACTORS`. Idempotent.

    Called at first :func:`lookup_extractor` invocation. Keeps the package
    import side-effect-free until something actually asks for an
    extractor (matters for tests that import ``state.paths`` without
    needing the extractors).
    """
    if EXTRACTORS:
        return
    # pylint: disable=import-outside-toplevel
    from state.language_extractors.cpp_extractor import CppExtractor
    from state.language_extractors.py_extractor import PyExtractor
    from state.language_extractors.ts_extractor import TsExtractor

    EXTRACTORS.extend([CppExtractor(), PyExtractor(), TsExtractor()])


def _regex_extractor() -> LanguageExtractor:
    """Lazy-import :class:`RegexExtractor`."""
    # pylint: disable=import-outside-toplevel
    from state.language_extractors.regex_extractor import RegexExtractor

    return RegexExtractor()


def lookup_extractor(suffix: str) -> LanguageExtractor:
    """Return the extractor responsible for files with *suffix*.

    Args:
        suffix: Lowercase file suffix including the leading dot
            (e.g. ``".py"``).

    Returns:
        The first extractor in :data:`EXTRACTORS` whose ``extensions``
        set contains *suffix*, or :class:`RegexExtractor` (lang_tag
        ``"other"``) when none claim it.
    """
    _populate_extractors()
    for ext in EXTRACTORS:
        if suffix in ext.extensions:
            return ext
    return _regex_extractor()


__all__ = [
    "EXTRACTORS",
    "ExtractedSymbol",
    "LanguageExtractor",
    "lookup_extractor",
]
