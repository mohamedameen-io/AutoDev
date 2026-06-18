#!/bin/bash
# Regression test for task_007_py_csv_feature.
# Exits 0 once to_csv is implemented; non-zero (import error) without it.
set -e
cd "$(dirname "$0")/repo"
python -m pytest test_tabular.py -q
