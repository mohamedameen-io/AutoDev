"""Shared I/O helpers for QA scanners.

v0.26.1 (patch A): centralise the user-source ``read_text`` contract so
every scanner survives non-UTF-8 input (Latin-1 surnames, copyright
glyphs, mixed encoding ASCII art, etc.). Prior to this helper three
scanners — :mod:`qa.cpp_symbols`, :func:`qa.hallucination_guard.\
_scan_python_file`, and :func:`qa.hallucination_guard._scan_typescript_file`
— wrapped ``read_text(encoding="utf-8")`` in ``except OSError`` only, so
the first non-UTF-8 byte in a vendored tree crashed the whole gate
walk (the 2026-05-11 Unity / SDL2 ``Charrière`` regression).

Contract:

* Success → returns the decoded text. Non-UTF-8 bytes are substituted
  with the Unicode replacement character (``U+FFFD``) via
  ``errors="replace"``.
* Path that is not a regular readable file (missing, directory,
  permission denied) → returns ``None``. Callers distinguish "scan
  nothing" from "empty file" via the ``None`` return.

Catches ``OSError`` (missing / permission / device errors) and
``ValueError`` (raised by ``Path.read_text`` on embedded NUL bytes and
similar edge cases). ``UnicodeDecodeError`` is intentionally NOT
caught — ``errors="replace"`` is sufficient and would not raise in the
first place.
"""

from __future__ import annotations

from pathlib import Path


def safe_read_source(path: Path) -> str | None:
    """Read ``path`` as text with replacement decoding.

    Returns ``None`` if the file cannot be read for any reason. See
    module docstring for the full contract.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


__all__ = ["safe_read_source"]
