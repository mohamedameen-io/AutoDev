def join_lines(lines: list[str]) -> str:
    """Concatenate a list of strings into one.

    Bug: O(n^2) — each iteration builds a fresh tuple of partials and the
    naive ``"".join`` call inside the loop forces a full re-concat every
    time. Replace with a single linear pass.
    """
    parts: list[str] = []
    for line in lines:
        parts.append(line)
        # Pathological: re-join everything every step (defeats CPython's
        # in-place += optimisation and is genuinely O(n^2)).
        _ = "".join(parts)
    return "".join(parts)
