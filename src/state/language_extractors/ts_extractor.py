"""TypeScript / JavaScript symbol extractor.

Tree-sitter when ``tree-sitter-typescript`` is installed, regex fallback
otherwise. Mirrors the optional-import pattern at
``qa/cpp_symbols.py:32-42``: probe at module import time, set a capability
flag, and route per call.

The regex fallback handles the common patterns that ground real-world JS
/ TS — ``export function``, ``export class``, ``export interface``,
``export const``, ``export type``, plain ``function name(...)``, plain
``class Name``. False-positives on the candidate digest are cheap (extra
choices for the architect); false-negatives are the cost we can't avoid
without a parser.
"""

from __future__ import annotations

import re

from state.language_extractors import ExtractedSymbol


_SIGNATURE_MAX_CHARS = 120


# v0.25.0: optional native binding. Capability flag mirrors the
# qa.cpp_symbols pattern.
try:  # pragma: no cover - native binding presence varies by platform
    import tree_sitter_typescript  # type: ignore[import-not-found,import-untyped]
    from tree_sitter import Language, Parser  # type: ignore[import-not-found,import-untyped]

    _TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
    _TS_PARSER = Parser(_TS_LANGUAGE)
    try:
        _TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())
        _TSX_PARSER = Parser(_TSX_LANGUAGE)
    except Exception:  # noqa: BLE001
        _TSX_LANGUAGE = None
        _TSX_PARSER = None
    TS_TREESITTER_AVAILABLE = True
except Exception:  # noqa: BLE001
    _TS_LANGUAGE = None  # type: ignore[assignment]
    _TS_PARSER = None  # type: ignore[assignment]
    _TSX_LANGUAGE = None  # type: ignore[assignment]
    _TSX_PARSER = None  # type: ignore[assignment]
    TS_TREESITTER_AVAILABLE = False


# Regex fallback patterns. Anchored to start-of-line (``^\s*``) so we don't
# pick up tokens deep inside a string literal or template.
_RE_EXPORT_FUNCTION = re.compile(
    r"^\s*export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
)
_RE_EXPORT_CLASS = re.compile(
    r"^\s*export\s+(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)\b"
)
_RE_EXPORT_INTERFACE = re.compile(
    r"^\s*export\s+interface\s+([A-Za-z_$][\w$]*)\b"
)
_RE_EXPORT_TYPE = re.compile(
    r"^\s*export\s+type\s+([A-Za-z_$][\w$]*)\b"
)
_RE_EXPORT_CONST = re.compile(
    r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b"
)
_RE_EXPORT_ENUM = re.compile(
    r"^\s*export\s+(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)\b"
)
_RE_EXPORT_DEFAULT_FN = re.compile(
    r"^\s*export\s+default\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
)
_RE_EXPORT_DEFAULT_CLASS = re.compile(
    r"^\s*export\s+default\s+class\s+([A-Za-z_$][\w$]*)\b"
)
_RE_PLAIN_FUNCTION = re.compile(
    r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
)
_RE_PLAIN_CLASS = re.compile(r"^\s*class\s+([A-Za-z_$][\w$]*)\b")
_RE_PLAIN_INTERFACE = re.compile(r"^\s*interface\s+([A-Za-z_$][\w$]*)\b")


def _trim_signature(line: str) -> str:
    line = line.strip()
    if len(line) > _SIGNATURE_MAX_CHARS:
        return line[: _SIGNATURE_MAX_CHARS - 1] + "…"
    return line


def _extract_via_regex(source: str) -> list[ExtractedSymbol]:
    out: list[ExtractedSymbol] = []
    seen: set[tuple[str, int]] = set()

    for line_idx, line in enumerate(source.splitlines(), start=1):
        for kind, regex in (
            ("function", _RE_EXPORT_FUNCTION),
            ("function", _RE_EXPORT_DEFAULT_FN),
            ("function", _RE_PLAIN_FUNCTION),
            ("class", _RE_EXPORT_CLASS),
            ("class", _RE_EXPORT_DEFAULT_CLASS),
            ("class", _RE_PLAIN_CLASS),
            ("class", _RE_EXPORT_INTERFACE),
            ("class", _RE_PLAIN_INTERFACE),
            ("var", _RE_EXPORT_TYPE),
            ("var", _RE_EXPORT_CONST),
            ("enum", _RE_EXPORT_ENUM),
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
                # Once a regex matches, do not let later regexes match the
                # same line (they would alias name + lineno).
                break
    return out


def _extract_via_treesitter(
    source: str, parser_choice: str
) -> list[ExtractedSymbol] | None:  # pragma: no cover - exercised only when binding present
    if not TS_TREESITTER_AVAILABLE:
        return None
    parser = _TSX_PARSER if parser_choice == "tsx" else _TS_PARSER
    if parser is None:
        return None
    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    out: list[ExtractedSymbol] = []
    seen: set[tuple[str, int]] = set()
    lines = source.splitlines()

    def _line_for(lineno: int) -> str:
        idx = lineno - 1
        if 0 <= idx < len(lines):
            return lines[idx]
        return ""

    cursor = tree.walk()

    def _walk() -> None:
        node = cursor.node
        if node is None:
            return
        kind: str | None = None
        if node.type in {
            "function_declaration",
            "function_signature",
            "method_definition",
        }:
            kind = "method" if node.type == "method_definition" else "function"
        elif node.type == "class_declaration":
            kind = "class"
        elif node.type == "interface_declaration":
            kind = "class"
        elif node.type == "type_alias_declaration":
            kind = "var"
        elif node.type == "enum_declaration":
            kind = "enum"
        elif node.type == "lexical_declaration":
            # ``export const X = ...`` arrives as a lexical_declaration
            # with a variable_declarator child whose name is an
            # identifier. Only emit the top-level case.
            for child in node.children:
                if child.type == "variable_declarator":
                    for sub in child.children:
                        if sub.type == "identifier" and sub.text is not None:
                            name = sub.text.decode("utf-8", errors="replace")
                            line = sub.start_point[0] + 1
                            col = sub.start_point[1]
                            key = (name, line)
                            if key not in seen:
                                seen.add(key)
                                out.append(
                                    ExtractedSymbol(
                                        name=name,
                                        kind="var",
                                        line=line,
                                        col=col,
                                        signature=_trim_signature(
                                            _line_for(line)
                                        ),
                                    )
                                )
                            break

        if kind is not None:
            for child in node.children:
                if child.type in {
                    "identifier",
                    "type_identifier",
                    "property_identifier",
                }:
                    if child.text is None:
                        continue
                    name = child.text.decode("utf-8", errors="replace")
                    line = child.start_point[0] + 1
                    col = child.start_point[1]
                    key = (name, line)
                    if key not in seen:
                        seen.add(key)
                        out.append(
                            ExtractedSymbol(
                                name=name,
                                kind=kind,
                                line=line,
                                col=col,
                                signature=_trim_signature(_line_for(line)),
                            )
                        )
                    break

        if cursor.goto_first_child():
            while True:
                _walk()
                if not cursor.goto_next_sibling():
                    break
            cursor.goto_parent()

    _walk()
    return out


class TsExtractor:
    """:class:`LanguageExtractor` for TypeScript / JavaScript files."""

    extensions: frozenset[str] = frozenset(
        {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    )
    lang_tag: str = "ts"

    def extract(
        self, source: str, *, parser_choice: str = "ts"
    ) -> list[ExtractedSymbol]:
        ts_symbols = _extract_via_treesitter(source, parser_choice)
        if ts_symbols is not None:
            return ts_symbols
        return _extract_via_regex(source)


__all__ = ["TS_TREESITTER_AVAILABLE", "TsExtractor"]
