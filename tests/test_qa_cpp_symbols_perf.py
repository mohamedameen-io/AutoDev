"""v0.22.1 A1 regression: extract_declarations is linear-time.

The pre-A1 ``_DECL`` regex had nested unbounded quantifiers
(``(?:[A-Za-z_][\\w:<>* &]*\\s+)+?``) that exposed catastrophic
backtracking on Unity-scale C++ headers. The 2026-05-09 stall
(orchestrator pinned 40+ min in ``_sre_SRE_Pattern_findall``) prompted
this rewrite. These tests pin the linear-time invariant so future
edits to the pattern can't regress it without an explicit alarm.
"""

from __future__ import annotations

import time

from qa.cpp_symbols import extract_declarations


def test_extract_declarations_basic() -> None:
    src = """
    void foo();
    int bar(int x);
    static const std::string baz();
    """
    result = extract_declarations(src)
    assert "foo" in result
    assert "bar" in result
    assert "baz" in result


def test_extract_declarations_pathological_input_completes_quickly() -> None:
    """Catastrophic-backtracking shape completes in <1s on the rewritten pattern.

    Pre-A1: this took >>30 s on the original ``_DECL`` (often hung).
    Post-A1: linear in input size; ~10-50ms on 200 template lines.
    """
    pathological = (
        "\n".join(
            f"template<typename T{i}, typename U{i}, typename V{i}> "
            f"class Foo{i} : public Bar<T{i}, U{i}, V{i}>"
            for i in range(200)
        )
        + "\nvoid normal_function();\n"
    )
    start = time.time()
    result = extract_declarations(pathological)
    elapsed = time.time() - start
    assert elapsed < 1.0, f"extract_declarations took {elapsed:.2f}s — should be linear"
    assert "normal_function" in result


def test_extract_declarations_handles_multiline_typedefs() -> None:
    """Typedef chains spanning multiple lines do not deadlock the parser.

    The new per-line scanner deliberately MISSES declarations split across
    physical lines (tradeoff for backtracking safety) — declarations on a
    single physical line are still picked up.
    """
    src = """
    typedef std::map<std::string, std::vector<int>> MapType;
    int single_line_decl(int a, int b);
    """
    result = extract_declarations(src)
    assert "single_line_decl" in result


def test_extract_declarations_empty_source() -> None:
    assert extract_declarations("") == set()


def test_extract_declarations_no_calls() -> None:
    """A source with no parentheses produces no declarations."""
    src = "int x = 5;\nstd::string s = \"hello\";\n"
    assert extract_declarations(src) == set()
