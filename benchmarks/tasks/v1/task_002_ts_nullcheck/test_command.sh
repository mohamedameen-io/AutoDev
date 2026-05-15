#!/bin/bash
# Regression test for task_002_ts_nullcheck.
# Exits 0 with the fix applied; non-zero without.
set -e
cd "$(dirname "$0")/repo"
node test_index.js
