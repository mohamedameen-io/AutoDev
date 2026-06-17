"""Tiny helpers for working with lists of dict "rows"."""


def column_names(rows: list[dict]) -> list[str]:
    """Return the column names (keys of the first row), or [] when empty."""
    if not rows:
        return []
    return list(rows[0].keys())
