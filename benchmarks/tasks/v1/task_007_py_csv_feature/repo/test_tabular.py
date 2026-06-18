"""Tests for the tabular helpers, including the to_csv feature."""

from tabular import column_names, to_csv


def test_column_names():
    assert column_names([{"a": 1, "b": 2}]) == ["a", "b"]
    assert column_names([]) == []


def test_to_csv_basic():
    rows = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
    assert to_csv(rows) == "name,age\nAlice,30\nBob,25\n"


def test_to_csv_single_column():
    assert to_csv([{"x": 1}, {"x": 2}]) == "x\n1\n2\n"


def test_to_csv_empty():
    assert to_csv([]) == ""
