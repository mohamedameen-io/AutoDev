from main import format_price


def test_format_price_int():
    assert format_price(42) == "$42.00"


def test_format_price_float():
    assert format_price(42.5) == "$42.50"
