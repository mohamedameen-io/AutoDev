"""Numeric helpers."""


def percentage(part: float, whole: float) -> float:
    """Return ``part`` as a percentage of ``whole``.

    Bug: returns the raw fraction (``part / whole``) instead of a percentage —
    the ``* 100`` is missing, so ``percentage(50, 200)`` yields ``0.25`` rather
    than ``25.0``.
    """
    if whole == 0:
        return 0.0
    return part / whole
