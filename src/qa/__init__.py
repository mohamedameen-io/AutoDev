"""QA gates for autodev Phase 8.

Built-in gates that run after the coder produces a diff and before the
reviewer sees it. Each gate is async and returns a :class:`GateResult`.
"""

from __future__ import annotations

from plugins.registry import GateResult
from qa.build_check import run_build_check
from qa.detect import detect_language, detect_toolchain
from qa.lint import run_lint
from qa.secretscan import run_secretscan
from qa.syntax_check import run_syntax_check
from qa.test_runner import run_tests

__all__ = [
    "GateResult",
    "detect_language",
    "detect_toolchain",
    "run_build_check",
    "run_lint",
    "run_secretscan",
    "run_syntax_check",
    "run_tests",
]
