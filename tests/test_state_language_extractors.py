"""Tests for :mod:`state.language_extractors` (v0.25.0)."""

from __future__ import annotations

import importlib.util

import pytest

from state.language_extractors import lookup_extractor
from state.language_extractors.cpp_extractor import CppExtractor
from state.language_extractors.py_extractor import PyExtractor
from state.language_extractors.regex_extractor import RegexExtractor
from state.language_extractors.ts_extractor import (
    TS_TREESITTER_AVAILABLE,
    TsExtractor,
)


def test_py_extractor_finds_functions_classes() -> None:
    src = (
        "import os\n"
        "\n"
        "TOP_CONST = 1\n"
        "\n"
        "def foo(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "async def bar():\n"
        "    pass\n"
        "\n"
        "class Baz:\n"
        "    def method(self):\n"
        "        return 0\n"
    )
    out = PyExtractor().extract(src)
    names = {(s["name"], s["kind"]) for s in out}
    assert ("foo", "function") in names
    assert ("bar", "function") in names
    assert ("Baz", "class") in names
    assert ("method", "method") in names
    assert ("TOP_CONST", "var") in names


def test_cpp_extractor_finds_declarations() -> None:
    src = (
        "// header\n"
        "namespace foo {\n"
        "class Bar {\n"
        "  void method();\n"
        "};\n"
        "struct S { int x; };\n"
        "void free_function(int x) {\n"
        "  return;\n"
        "}\n"
    )
    out = CppExtractor().extract(src)
    names = {(s["name"], s["kind"]) for s in out}
    # Regex fallback is conservative: it picks up class/struct/namespace
    # heads + the obvious free-function definition. We assert the
    # high-signal subset.
    assert ("foo", "namespace") in names
    assert ("Bar", "class") in names
    assert ("S", "struct") in names
    assert ("free_function", "function") in names


@pytest.mark.skipif(
    not TS_TREESITTER_AVAILABLE
    or importlib.util.find_spec("tree_sitter_typescript") is None,
    reason="tree-sitter-typescript not installed",
)
def test_ts_extractor_with_treesitter() -> None:  # pragma: no cover
    src = (
        "export function greet(name: string): string {\n"
        "  return `Hello, ${name}!`;\n"
        "}\n"
        "\n"
        "export class Logger {\n"
        "  log(msg: string) { console.log(msg); }\n"
        "}\n"
    )
    out = TsExtractor().extract(src)
    names = {(s["name"], s["kind"]) for s in out}
    assert ("greet", "function") in names
    assert ("Logger", "class") in names


def test_ts_extractor_regex_fallback() -> None:
    src = (
        "export function greet(name) {\n"
        "  return 'hi';\n"
        "}\n"
        "\n"
        "export class Logger {}\n"
        "\n"
        "export interface IFoo {}\n"
        "\n"
        "export const x = 1;\n"
        "\n"
        "function localFn() { return 0; }\n"
    )
    # Force the regex path even when tree-sitter is installed by calling
    # the regex helper directly.
    from state.language_extractors.ts_extractor import _extract_via_regex

    out = _extract_via_regex(src)
    names = {(s["name"], s["kind"]) for s in out}
    assert ("greet", "function") in names
    assert ("Logger", "class") in names
    assert ("IFoo", "class") in names
    assert ("x", "var") in names
    assert ("localFn", "function") in names


def test_regex_extractor_unknown_language() -> None:
    """Catch-all regex extractor handles e.g. Go / Rust / Ruby shapes."""
    src = (
        "// some made-up file\n"
        "func DoThing(x int) error {\n"
        "  return nil\n"
        "}\n"
        "\n"
        "fn rust_thing(x: i32) -> i32 {\n"
        "  x + 1\n"
        "}\n"
        "\n"
        "def python_thing(x):\n"
        "  return x\n"
        "\n"
        "class TopClass:\n"
        "  pass\n"
    )
    out = RegexExtractor().extract(src)
    names = {(s["name"], s["kind"]) for s in out}
    assert ("DoThing", "function") in names
    assert ("rust_thing", "function") in names
    assert ("python_thing", "function") in names
    assert ("TopClass", "class") in names


def test_lookup_extractor_routes_by_suffix() -> None:
    """Smoke test: dispatcher returns the right extractor per suffix."""
    assert lookup_extractor(".py").lang_tag == "py"
    assert lookup_extractor(".cpp").lang_tag == "cpp"
    assert lookup_extractor(".ts").lang_tag == "ts"
    assert lookup_extractor(".tsx").lang_tag == "ts"
    assert lookup_extractor(".rb").lang_tag == "other"
    assert lookup_extractor(".unknown").lang_tag == "other"
