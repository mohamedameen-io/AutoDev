import csv
import os
import tempfile

from parser import parse_csv_rows


def _write_csv(rows: list[list[str]]) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    return path


def test_parse_returns_all_data_rows():
    path = _write_csv([["name", "age"], ["Alice", "30"], ["Bob", "25"]])
    try:
        assert parse_csv_rows(path) == [["Alice", "30"], ["Bob", "25"]]
    finally:
        os.unlink(path)


def test_parse_single_data_row():
    path = _write_csv([["name", "age"], ["Solo", "1"]])
    try:
        assert parse_csv_rows(path) == [["Solo", "1"]]
    finally:
        os.unlink(path)
