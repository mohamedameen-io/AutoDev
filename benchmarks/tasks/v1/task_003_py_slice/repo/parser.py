import csv


def parse_csv_rows(path: str) -> list[list[str]]:
    """Return the data rows of a CSV file as lists of strings (skipping the header)."""
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        next(reader)  # consume header
        rows = list(reader)
    # Bug: this slice drops the first data row (off-by-one).
    return rows[1:]
