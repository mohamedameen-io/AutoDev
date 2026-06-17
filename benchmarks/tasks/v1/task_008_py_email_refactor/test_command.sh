#!/bin/bash
# Regression test for task_008_py_email_refactor.
# Behaviour-preserving: passes BEFORE and AFTER the refactor. The benchmark's
# structural-change guard (meta task_type=refactor) rejects a vacuous no-op.
set -e
cd "$(dirname "$0")/repo"
python -m pytest test_email.py -q
