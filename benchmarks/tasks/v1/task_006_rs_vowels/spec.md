# task_006_rs_vowels — count_vowels omits 'u'

`count_vowels()` in `src/lib.rs` is meant to count the five vowels
(a, e, i, o, u) in a string, case-insensitively. The match arm for `'u'` is
missing, so any string containing 'u' is under-counted.

Add the missing vowel so all five are counted.

## Expected behaviour
- `count_vowels("aeiou") == 5`
- `count_vowels("Ununium") == 4`
- `count_vowels("rhythm") == 0`

## How to verify
Run `bash test_command.sh` from this repo root. It must exit `0`. (Requires a
Rust toolchain; without `cargo` the per-language runner must degrade LOUD —
`skipped_toolchain_missing` — never silently pass.)
