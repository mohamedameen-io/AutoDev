# task_001_py_typeerror — fix the price formatter

The `format_price()` function in `main.py` raises a `TypeError` when called
with a `float` value. It should accept both `int` and `float` and format
either as a two-decimal currency string.

## Expected behaviour

```
format_price(42)    -> "$42.00"
format_price(42.5)  -> "$42.50"
```

## How to verify

Run `bash test_command.sh` from this repo root. It must exit `0`.
