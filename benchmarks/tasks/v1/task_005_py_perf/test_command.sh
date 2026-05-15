#!/bin/bash
# Regression test for task_005_py_perf.
# Exits 0 with the fix applied; non-zero without.
set -e
cd "$(dirname "$0")/repo"
python -m pytest test_joiner.py -q
