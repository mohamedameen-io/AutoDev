"""C++ symbol extractor — tree-sitter when available, regex fallback.

Reuses :func:`qa.cpp_symbols.extract_declarations` (the v0.22.1 A1 single-
line scan that bounds backtracking) for the regex fallback. When
``tree-sitter-cpp`` is installed we additionally walk the AST to pick up
class/struct/namespace/enum declarations that the regex pass misses.

Both paths yield :class:`ExtractedSymbol` dicts. Best-effort: any parse
failure returns ``[]``.
"""

from __future__ import annotations

import re

from qa.cpp_symbols import TREESITTER_AVAILABLE, extract_declarations
from state.language_extractors import ExtractedSymbol


_SIGNATURE_MAX_CHARS = 120

# Regex picks up class/struct/namespace/enum heads on a single line. Tight
# bounds (``{1,32}`` for the type name) avoid backtracking on huge headers.
_RE_CLASS = re.compile(r"^\s*class\s+([A-Za-z_]\w{0,63})\b")
_RE_STRUCT = re.compile(r"^\s*struct\s+([A-Za-z_]\w{0,63})\b")
_RE_NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_]\w{0,63})\b")
_RE_ENUM = re.compile(r"^\s*enum(?:\s+class)?\s+([A-Za-z_]\w{0,63})\b")
# Function declaration / definition. Mirrors the bounded pattern used by
# qa.cpp_symbols._DECL_LINE so behaviour is consistent.
_RE_FUNC = re.compile(
    r"^\s*(?:[A-Za-z_][\w:<>*&]*\s+){1,8}?([A-Za-z_]\w*)\s*\("
)


def _trim_signature(line: str) -> str:
    """Bound a declaration line to fit ``signature``."""
    line = line.strip()
    if len(line) > _SIGNATURE_MAX_CHARS:
        return line[: _SIGNATURE_MAX_CHARS - 1] + "…"
    return line


def _extract_via_regex(source: str) -> list[ExtractedSymbol]:
    """Per-line regex pass. Single source of truth for the fallback path."""
    out: list[ExtractedSymbol] = []
    seen: set[tuple[str, int]] = set()
    for line_idx, line in enumerate(source.splitlines(), start=1):
        for kind, regex in (
            ("class", _RE_CLASS),
            ("struct", _RE_STRUCT),
            ("namespace", _RE_NAMESPACE),
            ("enum", _RE_ENUM),
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
        # Function fallback: only emit when the line did NOT already match
        # a class/struct/namespace/enum (the type-header regex would
        # otherwise grab ``class Foo : public Bar { void method() {`` and
        # mis-emit ``Bar`` as a function).
        m_func = _RE_FUNC.match(line)
        if m_func is not None:
            name = m_func.group(1)
            # Avoid matching control-flow keywords used like calls in regex
            # mode. Mirror the qa.cpp_symbols._CPP_KEYWORDS subset most
            # likely to surface here.
            if name in {
                "if",
                "for",
                "while",
                "switch",
                "return",
                "do",
                "throw",
                "catch",
                "sizeof",
            }:
                continue
            # Skip when the line is actually a class/struct/namespace head
            # that already produced a symbol — the regex above matches
            # ``class Foo {`` because of the ``Foo {`` shape.
            if any(
                regex.match(line)
                for regex in (_RE_CLASS, _RE_STRUCT, _RE_NAMESPACE, _RE_ENUM)
            ):
                continue
            key = (name, line_idx)
            if key not in seen:
                seen.add(key)
                out.append(
                    ExtractedSymbol(
                        name=name,
                        kind="function",
                        line=line_idx,
                        col=m_func.start(1),
                        signature=_trim_signature(line),
                    )
                )
    # Cross-check against extract_declarations to ensure we capture the
    # canonical set the v0.22.1 A1 scanner reports — the per-line
    # function regex above is a superset, but we reconcile to keep the
    # extractor's output stable across qa.cpp_symbols revisions.
    canonical = extract_declarations(source)
    have = {sym["name"] for sym in out if sym["kind"] == "function"}
    for missing in canonical - have:
        # Locate the first line containing the name; signature is that
        # line. Defensive: skip if the locate fails.
        for line_idx, line in enumerate(source.splitlines(), start=1):
            if re.search(rf"\b{re.escape(missing)}\b\s*\(", line):
                key = (missing, line_idx)
                if key not in seen:
                    seen.add(key)
                    out.append(
                        ExtractedSymbol(
                            name=missing,
                            kind="function",
                            line=line_idx,
                            col=line.find(missing),
                            signature=_trim_signature(line),
                        )
                    )
                break
    return out


def _extract_via_treesitter(source: str) -> list[ExtractedSymbol] | None:
    """Tree-sitter walk. Returns ``None`` when the binding is unavailable."""
    if not TREESITTER_AVAILABLE:  # pragma: no cover - covered by has-binding env
        return None
    try:  # pragma: no cover - exercised only when the binding is present
        from qa.cpp_symbols import _CPP_PARSER  # type: ignore[attr-defined]

        if _CPP_PARSER is None:
            return None
        tree = _CPP_PARSER.parse(source.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    out: list[ExtractedSymbol] = []
    seen: set[tuple[str, int]] = set()
    lines = source.splitlines()

    def _line_for(byte_offset: int, lineno: int) -> str:
        idx = lineno - 1
        if 0 <= idx < len(lines):
            return lines[idx]
        return ""

    cursor = tree.walk()  # pragma: no cover

    def _walk() -> None:  # pragma: no cover
        node = cursor.node
        if node is None:
            return
        kind: str | None = None
        if node.type == "function_definition":
            kind = "function"
        elif node.type == "class_specifier":
            kind = "class"
        elif node.type == "struct_specifier":
            kind = "struct"
        elif node.type == "namespace_definition":
            kind = "namespace"
        elif node.type == "enum_specifier":
            kind = "enum"
        if kind is not None:
            ident = None
            for child in node.children:
                if child.type in {
                    "identifier",
                    "type_identifier",
                    "field_identifier",
                    "namespace_identifier",
                    "function_declarator",
                }:
                    if child.type == "function_declarator":
                        for sub in child.children:
                            if sub.type in {
                                "identifier",
                                "field_identifier",
                            }:
                                ident = sub
                                break
                    else:
                        ident = child
                    if ident is not None:
                        break
            if ident is not None and ident.text is not None:
                name = ident.text.decode("utf-8", errors="replace")
                line = ident.start_point[0] + 1
                col = ident.start_point[1]
                key = (name, line)
                if key not in seen:
                    seen.add(key)
                    out.append(
                        ExtractedSymbol(
                            name=name,
                            kind=kind,
                            line=line,
                            col=col,
                            signature=_trim_signature(
                                _line_for(ident.start_byte, line)
                            ),
                        )
                    )
        if cursor.goto_first_child():
            while True:
                _walk()
                if not cursor.goto_next_sibling():
                    break
            cursor.goto_parent()

    _walk()
    return out


class CppExtractor:
    """:class:`LanguageExtractor` for C/C++ source files."""

    extensions: frozenset[str] = frozenset(
        {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"}
    )
    lang_tag: str = "cpp"

    def extract(self, source: str) -> list[ExtractedSymbol]:
        ts_symbols = _extract_via_treesitter(source)
        if ts_symbols is not None:
            return ts_symbols
        return _extract_via_regex(source)


__all__ = ["CppExtractor"]
