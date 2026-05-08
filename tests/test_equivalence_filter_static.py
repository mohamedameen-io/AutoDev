"""Tests for v0.19.0 Stage 1 static equivalence filter."""

from __future__ import annotations

from qa.equivalence_filter import (
    is_static_equivalent,
    normalize_python_source,
)


def test_identical_text_equivalent() -> None:
    assert is_static_equivalent("x = 1", "x = 1")


def test_whitespace_only_difference_equivalent() -> None:
    assert is_static_equivalent("x = 1", "x  =  1\n")


def test_comment_only_difference_equivalent() -> None:
    assert is_static_equivalent("x = 1  # foo", "x = 1  # bar")
    assert is_static_equivalent("x = 1  # comment", "x = 1")


def test_real_code_change_not_equivalent() -> None:
    assert not is_static_equivalent("x = 1", "x = 2")


def test_operator_swap_not_equivalent() -> None:
    """``+`` to ``-`` is a mutmut staple — must be flagged as non-equivalent."""
    assert not is_static_equivalent("x = a + b", "x = a - b")


def test_const_replacement_not_equivalent() -> None:
    assert not is_static_equivalent("if x > 0:", "if x >= 0:")


def test_normalize_python_source_returns_canonical() -> None:
    a = normalize_python_source("x = 1")
    b = normalize_python_source("x  =  1   # comment")
    assert a == b
    assert a is not None


def test_normalize_returns_none_on_syntax_error() -> None:
    assert normalize_python_source("def foo(:") is None


def test_fstring_equivalence() -> None:
    """f-strings parse to the same AST regardless of formatting."""
    a = "f'value = {x}'"
    b = "f'value = {x}'"
    assert is_static_equivalent(a, b)


def test_walrus_equivalence() -> None:
    """Walrus operator (3.8+) parses correctly."""
    a = "if (n := 10) > 5: pass"
    b = "if (n := 10) > 5:  pass\n"
    assert is_static_equivalent(a, b)


def test_non_python_fallback_text_normalization() -> None:
    """Sources that don't parse as Python use textual normalization."""
    a = """function foo() {
        return 1; // legacy
    }"""
    b = """function foo() {
        return 1;
    }"""
    assert is_static_equivalent(a, b)


def test_non_python_fallback_real_change() -> None:
    a = "function foo() { return 1; }"
    b = "function foo() { return 2; }"
    assert not is_static_equivalent(a, b)
