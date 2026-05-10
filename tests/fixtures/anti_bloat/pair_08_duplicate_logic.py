"""Verbose: same 8-line normalization block repeated in 3 functions."""


def save_username(name: str) -> str:
    cleaned = name.strip()
    cleaned = cleaned.lower()
    cleaned = cleaned.replace(" ", "_")
    cleaned = "".join(c for c in cleaned if c.isalnum() or c == "_")
    if not cleaned:
        cleaned = "anonymous"
    if len(cleaned) > 32:
        cleaned = cleaned[:32]
    return f"user:{cleaned}"


def save_groupname(name: str) -> str:
    cleaned = name.strip()
    cleaned = cleaned.lower()
    cleaned = cleaned.replace(" ", "_")
    cleaned = "".join(c for c in cleaned if c.isalnum() or c == "_")
    if not cleaned:
        cleaned = "anonymous"
    if len(cleaned) > 32:
        cleaned = cleaned[:32]
    return f"group:{cleaned}"


def save_tagname(name: str) -> str:
    cleaned = name.strip()
    cleaned = cleaned.lower()
    cleaned = cleaned.replace(" ", "_")
    cleaned = "".join(c for c in cleaned if c.isalnum() or c == "_")
    if not cleaned:
        cleaned = "anonymous"
    if len(cleaned) > 32:
        cleaned = cleaned[:32]
    return f"tag:{cleaned}"
