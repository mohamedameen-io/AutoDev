"""Heuristic regex extractor for unknown file extensions.

Last-resort extractor used by :func:`state.language_extractors.lookup_extractor`
when no language-specific extractor claims a file's suffix. Picks up the
common ``def name(...)`` / ``function name(...)`` / ``class Name`` shapes
across most curly-brace and indentation-based languages so the file
inventory always has *some* symbols to feed the FTS index.

Conservative: lots of false-positives are fine (the architect just sees
a few extra candidates); false-negatives waste a candidate slot.
"""

from __future__ import annotations

import re

from state.language_extractors import ExtractedSymbol


_SIGNATURE_MAX_CHARS = 120

_RE_DEF = re.compile(r"^\s*def\s+([A-Za-z_]\w{0,63})\s*\(")
_RE_FUNC = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)?"
    r"function\s+([A-Za-z_$][\w$]{0,63})\s*\("
)
_RE_FUNC_GO = re.compile(r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w{0,63})\s*\(")
_RE_FUNC_RUST = re.compile(
    r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w{0,63})\s*[<(]"
)
_RE_CLASS = re.compile(
    r"^\s*(?:public\s+|private\s+|abstract\s+|final\s+|sealed\s+)?"
    r"(?:class|struct|trait|enum|interface)\s+([A-Za-z_$][\w$]{0,63})\b"
)


def _trim_signature(line: str) -> str:
    line = line.strip()
    if len(line) > _SIGNATURE_MAX_CHARS:
        return line[: _SIGNATURE_MAX_CHARS - 1] + "…"
    return line


class RegexExtractor:
    """:class:`LanguageExtractor` catch-all for unknown extensions."""

    extensions: frozenset[str] = frozenset()  # claims nothing by default
    lang_tag: str = "other"

    def extract(self, source: str) -> list[ExtractedSymbol]:
        out: list[ExtractedSymbol] = []
        seen: set[tuple[str, int]] = set()
        for line_idx, line in enumerate(source.splitlines(), start=1):
            for kind, regex in (
                ("function", _RE_DEF),
                ("function", _RE_FUNC),
                ("function", _RE_FUNC_GO),
                ("function", _RE_FUNC_RUST),
                ("class", _RE_CLASS),
            ):
                m = regex.match(line)
                if m is not None:
                    name = m.group(1)
                    key = (name, line_idx)
                    if key not in seen:
                        seen.add(key)
                        out.append(
                            ExtractedSymbol(
                                name=name,
                                kind=kind,
                                line=line_idx,
                                col=m.start(1),
                                signature=_trim_signature(line),
                            )
                        )
                    break
        return out


__all__ = ["RegexExtractor"]
