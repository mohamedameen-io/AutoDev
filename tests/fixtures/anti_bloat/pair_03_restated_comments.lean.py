"""Lean: well-named code, no restating comments."""


def normalize_truthy(items):
    return [str(item).strip().lower() for item in items if item]
