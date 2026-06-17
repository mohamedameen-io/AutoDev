from core.calc import percentage


def test_percentage_basic():
    assert percentage(50, 200) == 25.0


def test_percentage_half():
    assert percentage(1, 2) == 50.0


def test_percentage_zero_whole():
    assert percentage(5, 0) == 0.0
