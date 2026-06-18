"""Contact records."""


def _valid_email(addr: str) -> bool:
    # Triplicated across users.py / orders.py / contacts.py — extract me.
    if not isinstance(addr, str):
        return False
    if addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    return True


def add_contact(name: str, email: str) -> dict:
    return {"name": name, "email": email, "valid": _valid_email(email)}
