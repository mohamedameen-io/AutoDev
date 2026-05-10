"""Verbose: defensive scaffolding smell.

Wraps simple typed list summation in try/except and None checks for cases
the type annotation already prevents. LLMs add this 'just in case'.
"""
from typing import List


def sum_positive(numbers: List[int]) -> int:
    if numbers is None:
        return 0
    if not isinstance(numbers, list):
        return 0
    try:
        total = 0
        for n in numbers:
            if n is None:
                continue
            if not isinstance(n, int):
                continue
            try:
                if n > 0:
                    total += n
            except TypeError:
                continue
        return total
    except Exception:
        return 0
