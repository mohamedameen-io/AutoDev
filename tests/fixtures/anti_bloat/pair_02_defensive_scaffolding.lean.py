"""Lean: trust the type annotation."""
from typing import List


def sum_positive(numbers: List[int]) -> int:
    return sum(n for n in numbers if n > 0)
