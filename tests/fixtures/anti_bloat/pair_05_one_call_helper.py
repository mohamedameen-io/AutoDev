"""Verbose: tiny `_format_user` helper called from exactly one site."""


def _format_user(first: str, last: str) -> str:
    full = first.strip() + " " + last.strip()
    return full.title()


def _build_greeting(name: str) -> str:
    return "Hello, " + name + "!"


def greet(first: str, last: str) -> str:
    name = _format_user(first, last)
    return _build_greeting(name)
