"""Verbose: 5 config kwargs, only `multiplier` is actually used by callers."""


def scale(
    value: float,
    multiplier: float = 1.0,
    offset: float = 0.0,
    clamp_min: float = -1e9,
    clamp_max: float = 1e9,
    rounding: str = "none",
    precision: int = 6,
) -> float:
    result = value * multiplier
    result = result + offset
    if result < clamp_min:
        result = clamp_min
    if result > clamp_max:
        result = clamp_max
    if rounding == "round":
        result = round(result, precision)
    elif rounding == "floor":
        from math import floor
        result = floor(result)
    elif rounding == "ceil":
        from math import ceil
        result = ceil(result)
    return result
