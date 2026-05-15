def format_price(amount: int) -> str:
    # Bug: this guard rejects floats with a TypeError, but the spec says
    # we should accept both int and float.
    if not isinstance(amount, int):
        raise TypeError(f"format_price expects int, got {type(amount).__name__}")
    return f"${amount:.2f}"
