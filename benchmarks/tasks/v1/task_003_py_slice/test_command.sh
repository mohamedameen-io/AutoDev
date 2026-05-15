#!/bin/bash
# Regression test for task_003_py_slice.
# Exits 0 with the fix applied; non-zero without.
set -e
cd "$(dirname "$0")/repo"
python -m pytest test_parser.py -q
