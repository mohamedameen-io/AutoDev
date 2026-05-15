# task_003_py_slice — off-by-one in CSV header skip

`parse_csv_rows()` in `parser.py` is supposed to skip the header row and return
the data rows as lists of strings. It currently drops the first data row too
(off-by-one: it both calls `next(reader)` AND slices `[1:]`).

## Expected behaviour

Given the CSV:
```
name,age
Alice,30
Bob,25
```
`parse_csv_rows("data.csv")` should return:
```
[["Alice", "30"], ["Bob", "25"]]
```
(The current buggy code returns `[["Bob", "25"]]`.)

## How to verify

Run `bash test_command.sh` from this repo root. It must exit `0`.
