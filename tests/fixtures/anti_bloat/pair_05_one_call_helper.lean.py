"""Lean: helpers inlined at the single call site."""


def greet(first: str, last: str) -> str:
    return f"Hello, {(first.strip() + ' ' + last.strip()).title()}!"
