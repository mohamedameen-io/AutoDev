"""Lean: one helper called three times."""


def _slugify(name: str) -> str:
    cleaned = "".join(c for c in name.strip().lower().replace(" ", "_") if c.isalnum() or c == "_")
    return (cleaned or "anonymous")[:32]


def save_username(name: str) -> str:
    return f"user:{_slugify(name)}"


def save_groupname(name: str) -> str:
    return f"group:{_slugify(name)}"


def save_tagname(name: str) -> str:
    return f"tag:{_slugify(name)}"
