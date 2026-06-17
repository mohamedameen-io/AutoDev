# task_009_py_50k_scale — percentage() missing ×100 (large repo)

`percentage(part, whole)` in `core/calc.py` is supposed to return `part` as a
percentage of `whole`, but it returns the raw fraction — the `* 100` is
missing. `percentage(50, 200)` should be `25.0`, not `0.25`.

This repository is **large** (~50,000 Python modules under `filler/`), but the
bug is isolated to `core/calc.py`. Fix only that function.

## Expected behaviour
- `percentage(50, 200) == 25.0`
- `percentage(1, 2) == 50.0`
- `percentage(5, 0) == 0.0`  (guard against divide-by-zero)

## How to verify
Run `bash test_command.sh` from this repo root. It must exit `0`.
