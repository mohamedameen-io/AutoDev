# AutoDev developer Makefile.
#
# Two main entry points:
#
#   make test            — full pytest suite.
#   make test-stability  — fast suite over the v0.27-touched modules
#                          (parser, ledger, plan_phase). Target <10s.
#   make mutate-parser   — manual mutmut gate on plan_parser +
#                          path_validator. Not in CI.

.PHONY: test test-stability mutate-parser

test:
	uv run pytest tests/ -v

test-stability:
	uv run pytest -q \
	  tests/test_orchestrator_plan_parser.py \
	  tests/test_orchestrator_plan_parser_hedge_text.py \
	  tests/test_state_ledger_exhaustive_apply_op.py \
	  tests/test_orchestrator_plan_phase_drop.py \
	  tests/test_orchestrator_plan_phase_hedge_repro.py

mutate-parser:
	@command -v mutmut >/dev/null 2>&1 || { \
	  echo "mutmut not installed: run 'uv pip install mutmut' first"; \
	  exit 1; \
	}
	mutmut run \
	  --paths-to-mutate=src/orchestrator/plan_parser.py,src/orchestrator/path_validator.py \
	  --tests-dir=tests \
	  --runner='uv run pytest -x -q tests/test_orchestrator_plan_parser.py tests/test_orchestrator_plan_parser_hedge_text.py'
	mutmut results
