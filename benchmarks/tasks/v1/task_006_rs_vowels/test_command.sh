#!/bin/bash
# Regression test for task_006_rs_vowels.
# Exits 0 with the fix applied; non-zero without. Requires a Rust toolchain
# (cargo). On a host without cargo the per-language runner must degrade LOUD
# (skipped_toolchain_missing) rather than silently passing.
set -e
cd "$(dirname "$0")/repo"
cargo test --quiet
