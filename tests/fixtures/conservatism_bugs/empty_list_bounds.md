# Bug: IndexError on empty input

`src/stats.py:median` raises `IndexError` when given an empty list because it
indexes `values[n // 2]` without checking length. Guard against the empty case
and return `None` (or raise a clear `ValueError`).

Hypothesis: add a bounds check for the empty list.
