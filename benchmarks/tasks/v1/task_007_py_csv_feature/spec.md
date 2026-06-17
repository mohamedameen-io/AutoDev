# task_007_py_csv_feature — add CSV export (new feature)

`tabular.py` has helpers for working with lists of dict rows but cannot yet
serialise them to CSV. **Add a new function** `to_csv(rows)` that turns a list
of dict rows into a CSV string.

This is a NEW capability, not a bug fix — there is no existing `to_csv` to
repair.

## Required behaviour
- `to_csv(rows)` takes `rows: list[dict]`.
- The first output line is the header: the keys of the first row, comma-joined.
- Each subsequent line is one row's values (same column order), comma-joined
  and stringified.
- Every line (including the last) ends with `\n`.
- `to_csv([])` returns `""` (the empty string).

Example:

    to_csv([{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}])
    == "name,age\nAlice,30\nBob,25\n"

## How to verify
Run `bash test_command.sh` from this repo root. It must exit `0`.
