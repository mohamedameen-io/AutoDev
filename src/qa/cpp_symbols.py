"""C++ symbol resolution for hallucination-guard (v0.19.0).

Detects calls to functions that are never declared anywhere in the include
chain — the canonical C++ hallucination shape (the LLM invents a plausible
function name). Two-tier strategy:

  1. Tree-sitter-cpp AST when ``tree-sitter-cpp`` is installed (precise call
     extraction, qualified-call detection, macro literal recognition).
  2. Regex fallback when the native dependency is missing — coarser, but
     covers the common ``foo(args)`` shape and skips obvious noise
     (control-flow keywords, qualified ``ns::foo``, member ``.foo()``).

Conservative defaults: when in doubt, **pass**. False-positives on
real-world C++ are punitive (preprocessor + templates + ADL + unity-builds
make full semantic resolution infeasible at gate-time). This module's job is
to catch the obvious slop, not to be a compiler.

System headers (``<…>``) are excluded from include-chain resolution. Only
local quoted headers (``"…"``) participate. A symbol is "unresolved" only
when none of the local headers AND none of the source's own declarations
provide a matching identifier. This is deliberately permissive.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# v0.19.0: tree-sitter-cpp is OPTIONAL; the module degrades to regex when
# unavailable. Probe at import time to set the capability flag.
try:  # pragma: no cover - native binding presence varies by platform
    import tree_sitter_cpp  # type: ignore[import-not-found,import-untyped]
    from tree_sitter import Language, Parser  # type: ignore[import-not-found,import-untyped]

    _CPP_LANGUAGE = Language(tree_sitter_cpp.language())
    _CPP_PARSER = Parser(_CPP_LANGUAGE)
    TREESITTER_AVAILABLE = True
except Exception:  # noqa: BLE001 — broad: any import-time failure disables.
    _CPP_LANGUAGE = None  # type: ignore[assignment]
    _CPP_PARSER = None  # type: ignore[assignment]
    TREESITTER_AVAILABLE = False


# C++ control-flow / type / declaration keywords that look like calls in
# regex-mode but never are. Keep this list defensive — false-positives on
# keywords would surface as "hallucinated reference to ``if``", which
# would erode trust in the gate.
_CPP_KEYWORDS: frozenset[str] = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "default",
        "return",
        "break",
        "continue",
        "goto",
        "try",
        "catch",
        "throw",
        "sizeof",
        "alignof",
        "alignas",
        "typeid",
        "static_cast",
        "dynamic_cast",
        "reinterpret_cast",
        "const_cast",
        "new",
        "delete",
        "this",
        "true",
        "false",
        "nullptr",
        "operator",
        "noexcept",
        "decltype",
        "typedef",
        "struct",
        "class",
        "union",
        "enum",
        "namespace",
        "using",
        "template",
        "typename",
        "friend",
        "virtual",
        "explicit",
        "inline",
        "constexpr",
        "static",
        "extern",
        "thread_local",
        "mutable",
        "register",
        "volatile",
        "const",
        "auto",
        "void",
        "bool",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "wchar_t",
        "char16_t",
        "char32_t",
        "char8_t",
        "asm",
        "co_await",
        "co_return",
        "co_yield",
        "concept",
        "requires",
        "import",
        "module",
    }
)


_INCLUDE_LOCAL = re.compile(r'#\s*include\s*"([^"]+)"')
_INCLUDE_SYSTEM = re.compile(r"#\s*include\s*<([^>]+)>")
_DEFINE = re.compile(r"#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)")
# Unqualified call: identifier followed by ``(``, NOT preceded by ``::`` or ``.``
# or ``->``. Captures the identifier.
_CALL = re.compile(
    r"(?<![\w:.>])([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
# Identifier following ``::`` (qualified) — strip these so we don't mark
# ``foo`` from ``ns::foo()`` as unqualified.
_QUALIFIED_CALL = re.compile(r"::\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# Member access: ``.foo(`` or ``->foo(`` — likewise excluded.
_MEMBER_CALL = re.compile(r"(?:\.|->)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# v0.22.1 A1: ``_DECL_LINE`` replaces a multi-line pattern with nested
# unbounded quantifiers (``(?:[A-Za-z_][\w:<>* &]*\s+)+?``) that exposed
# catastrophic backtracking on Unity-scale C++ headers (2026-05-09 stall:
# 40+ min CPU pin in ``_sre_SRE_Pattern_findall``). The replacement scans
# one line at a time, bounds the type-token repeat to ``{1,8}?``, and
# drops the embedded space from the inner character class. Linear in
# input length.
_DECL_LINE = re.compile(
    r"^\s*(?:[A-Za-z_][\w:<>*&]*\s+){1,8}?([A-Za-z_]\w*)\s*\("
)


def extract_local_includes(source: str) -> list[str]:
    """Return the list of ``#include "…"`` paths in *source* (system excluded)."""
    return _INCLUDE_LOCAL.findall(source)


def extract_unqualified_calls(source: str) -> set[str]:
    """Return the set of unqualified call identifiers in *source*.

    Excludes:
      * Qualified calls (``ns::foo(...)``).
      * Member calls (``obj.foo(...)``, ``ptr->foo(...)``).
      * C++ keywords (``if``, ``for``, ``return`` …).
    """
    qualified = set(_QUALIFIED_CALL.findall(source))
    members = set(_MEMBER_CALL.findall(source))
    candidates = set(_CALL.findall(source))
    # The ``_CALL`` pattern already filters preceding ``::`` / ``.`` / ``->``,
    # but we additionally subtract the ``qualified`` and ``members`` sets to
    # be defensive (regex anchors can miss when whitespace is unusual).
    out: set[str] = set()
    for ident in candidates:
        if ident in _CPP_KEYWORDS:
            continue
        if ident in qualified or ident in members:
            continue
        out.add(ident)
    return out


def extract_declarations(source: str) -> set[str]:
    """Return the set of identifiers declared / defined as functions in *source*.

    Coarse — pattern-matches the common ``RET name(args)`` shape. Misses
    template specializations, operator overloads, and lambda assignments.
    The cost of a miss is a false positive on the call site, which we
    moderate via the include-chain breadth (any header providing the
    name suffices).

    v0.22.1 A1: scans one line at a time so backtracking cannot span
    multiple lines on long template / typedef chains.
    """
    out: set[str] = set()
    for line in source.splitlines():
        m = _DECL_LINE.match(line)
        if m is not None:
            out.add(m.group(1))
    return out


def extract_macros(source: str) -> set[str]:
    """Return the set of identifiers introduced by ``#define`` in *source*.

    Macros are skip-and-warn: when a call site's identifier matches a
    defined macro, we treat it as "expansion site beyond static reach"
    and do not emit a finding.
    """
    return set(_DEFINE.findall(source))


def resolve_include_chain(
    source_path: Path,
    repo_root: Path | None = None,
    seen: set[Path] | None = None,
) -> list[Path]:
    """Walk ``#include "…"`` directives recursively from *source_path*.

    Returns a list of header paths transitively included. System headers
    (``<…>``) are excluded — they would require parsing the toolchain's
    include path, which is out of scope for a static gate.

    The walk handles cycles via *seen* tracking and silently skips
    headers that don't exist on disk (the file being compiled may rely
    on a generator step that hasn't run; conservative pass).
    """
    if seen is None:
        seen = set()
    out: list[Path] = []
    try:
        resolved_self = source_path.resolve()
    except OSError:
        return out
    if resolved_self in seen:
        return out
    seen.add(resolved_self)

    parent = source_path.parent
    try:
        text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    for include in extract_local_includes(text):
        candidate = (parent / include).resolve()
        if candidate.exists() and candidate not in seen:
            out.append(candidate)
            out.extend(resolve_include_chain(candidate, repo_root, seen))
    return out


def _gather_chain_symbols(
    chain_paths: Iterable[Path],
) -> tuple[set[str], set[str]]:
    """Return (declared_symbols, macro_symbols) over the include chain."""
    decls: set[str] = set()
    macros: set[str] = set()
    for path in chain_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        decls |= extract_declarations(text)
        macros |= extract_macros(text)
    return decls, macros


def scan_cpp_file(path: Path, repo_root: Path) -> list[str]:
    """Return findings (unresolved unqualified calls) for a single C++ file.

    Tree-sitter is preferred when available (more accurate call extraction
    and macro/qualified-call detection). The regex fallback follows the
    same skip-and-warn semantics: when the source has only system-header
    includes (no local headers), we do NOT emit findings — system headers
    are not parsed.
    """
    from qa._io import safe_read_source

    text = safe_read_source(path)
    if text is None:
        return []

    local_includes = extract_local_includes(text)
    has_local_includes = bool(local_includes)

    # Skip-and-warn when there are no local headers to anchor resolution.
    # Without a local include chain we cannot decide what symbols are valid;
    # the file may rely on system headers (which we don't parse), generated
    # headers, or compiler intrinsics. Conservative pass.
    if not has_local_includes:
        return []

    # Build the symbol table from local headers + the source itself.
    chain = resolve_include_chain(path)
    chain_decls, chain_macros = _gather_chain_symbols(chain)
    own_decls = extract_declarations(text)
    own_macros = extract_macros(text)

    declared = chain_decls | own_decls
    macros = chain_macros | own_macros

    if TREESITTER_AVAILABLE:  # pragma: no cover - exercised only when dep present
        calls = _ts_extract_calls(text)
    else:
        calls = extract_unqualified_calls(text)

    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()

    findings: list[str] = []
    for call in sorted(calls):
        if call in declared or call in macros:
            continue
        if call in _CPP_KEYWORDS:
            continue
        findings.append(
            f"{rel}: hallucinated reference — "
            f"call to '{call}' has no matching declaration in include chain"
        )
    return findings


def _ts_extract_calls(source: str) -> set[str]:  # pragma: no cover
    """Tree-sitter-backed call extraction. Mirrors regex semantics."""
    if not TREESITTER_AVAILABLE or _CPP_PARSER is None:
        return extract_unqualified_calls(source)
    try:
        tree = _CPP_PARSER.parse(source.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return extract_unqualified_calls(source)
    out: set[str] = set()
    cursor = tree.walk()

    def _walk() -> None:
        node = cursor.node
        if node is None:
            return
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                if func.type == "identifier":
                    name = func.text.decode("utf-8") if func.text else ""
                    if name and name not in _CPP_KEYWORDS:
                        out.add(name)
        if cursor.goto_first_child():
            while True:
                _walk()
                if not cursor.goto_next_sibling():
                    break
            cursor.goto_parent()

    _walk()
    return out


__all__ = [
    "TREESITTER_AVAILABLE",
    "extract_declarations",
    "extract_local_includes",
    "extract_macros",
    "extract_unqualified_calls",
    "resolve_include_chain",
    "scan_cpp_file",
]
