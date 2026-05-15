#!/bin/bash
# Regression test for task_001_py_typeerror.
# Exits 0 with the fix applied; non-zero without.
set -e
cd "$(dirname "$0")/repo"
python -m pytest test_main.py -q
