#!/bin/bash
# Regression test for task_004_go_defer.
# Exits 0 with the fix applied; non-zero without.
set -e
cd "$(dirname "$0")/repo"
go test -count=1 ./...
