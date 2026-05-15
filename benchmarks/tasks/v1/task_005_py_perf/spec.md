# task_005_py_perf — quadratic string concatenation in join_lines

`join_lines()` in `joiner.py` is supposed to concatenate a list of strings.
The current implementation is O(n²) — it appends to a list and re-runs
`"".join(parts)` inside the loop on every iteration, which means each step
walks the entire growing buffer. On 50,000 lines this takes several seconds.

Refactor it to be linear. The simplest fix is `return "".join(lines)`.
Keep the public signature unchanged.

## Expected behaviour

`join_lines(lines)` returns the same string it does today (each input line
concatenated in order), but the perf test (50k lines, <1s) passes.

## How to verify

Run `bash test_command.sh` from this repo root. It must exit `0`.
