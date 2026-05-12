"""Malformed diff payloads for the v0.27 fail-closed QA-gate tests.

Each fixture is a synthetic developer-output diff string that triggers
the parser's "non-empty body, no parseable header" branch — the case
v0.26.2 silently swallowed and v0.27 fail-closes against.
"""

from __future__ import annotations


# A truncated diff: header started but cut off before any ``+++ b/`` line.
TRUNCATED_DIFF_HEADER_ONLY = (
    "diff --git a/src/math/__init__.py b/src/math/__init__.py\n"
    "index 1234..5678 100644\n"
    "--- a/src/math/__init__.py\n"
)


# Developer dumped prose where a diff should have been (LLM hallucination).
PROSE_INSTEAD_OF_DIFF = (
    "Here's the change I'd make:\n"
    "I would add a subtract function to src/math/__init__.py that "
    "performs subtraction in the natural way.\n"
)


# A JSON envelope without any diff field — the adapter forgot to extract.
JSON_WITHOUT_DIFF_FIELD = (
    '{"role": "developer", "files_changed": ["src/math/__init__.py"], '
    '"reasoning": "Added subtract."}\n'
)


# A diff body that uses the wrong header prefix (``Index:`` instead of
# ``+++ b/``) — common output from older SCM tools and from some
# developer prompts that ask for "any patch format".
WRONG_HEADER_PREFIX = (
    "Index: src/math/__init__.py\n"
    "===================================================================\n"
    "--- src/math/__init__.py\n"
    "+++ src/math/__init__.py\n"
    "@@ -1 +1,2 @@\n"
    "+def subtract(a, b): return a - b\n"
)


# Single-line garbage (BOM + a stray char). Below the 80-char preview
# threshold used in the error message so we can assert on the snippet.
BOM_GARBAGE = "﻿?\n"


ALL_MALFORMED_DIFFS = [
    ("truncated_diff_header_only", TRUNCATED_DIFF_HEADER_ONLY),
    ("prose_instead_of_diff", PROSE_INSTEAD_OF_DIFF),
    ("json_without_diff_field", JSON_WITHOUT_DIFF_FIELD),
    ("wrong_header_prefix", WRONG_HEADER_PREFIX),
    ("bom_garbage", BOM_GARBAGE),
]
