#!/bin/bash
# Regression test for task_009_py_50k_scale.
# Runs ONLY the needle test (the ~50k filler modules carry no tests and must
# not be collected). Exits 0 with the *100 fix; non-zero without.
set -e
cd "$(dirname "$0")/repo"
python -m pytest test_calc.py -q
