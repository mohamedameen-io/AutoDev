# Project Intent

## Goal

Add a `subtract(a, b)` function to `math.py` that returns the difference of two numbers.

## Constraints

- Python 3.11+
- The function must be in `math.py` alongside the existing `add(a, b)` function
- Follow the same style as `add`: type-annotated, docstring, simple implementation
- Export `subtract` from `__init__.py`

## Non-goals

- No multiplication, division, or other operations in this iteration
- No CLI interface

## Success criteria

- `subtract(5, 3)` returns `2`
- `subtract(0, 5)` returns `-5`
- `subtract(1.5, 0.5)` returns `1.0`
- `subtract` is importable from the package root
- At least one test covers the function
