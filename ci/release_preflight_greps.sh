#!/usr/bin/env bash
# Mirror of the grep-style preflights in .github/workflows/release.yml so
# the same checks can be run locally before pushing a release tag.
#
# Usage: ci/release_preflight_greps.sh [v0.32 | v0.33 | all]   (default: all)
#
# Each block locks the integration of a shipped phase against accidental
# removal. Adding a new release? Append a block and update the dispatch
# below.

set -euo pipefail

cd "$(dirname "$0")/.."

target="${1:-all}"

check() {
  local label="$1"
  local file="$2"
  local pattern="$3"
  if ! grep -q "$pattern" "$file"; then
    echo "FAIL [$label]: $file missing pattern '$pattern'"
    exit 1
  fi
  echo "ok   [$label]: $file contains '$pattern'"
}

preflight_v032() {
  check "v0.32 rejection_history" src/agents/prompts/architect.md "{rejection_history}"
  check "v0.32 RecoveryHint"      src/state/schemas.py            "class RecoveryHint"
  check "v0.32 recovery_hint cli" src/cli/commands/status.py       "recovery_hint"
}

preflight_v033() {
  check "v0.33 A1 helper"        src/orchestrator/file_existence_validator.py "_collect_plan_new_files"
  check "v0.33 A1 ledger op"     src/state/ledger.py                          "path_validation_resolved_via_plan_global"
  check "v0.33 A2 anchor"        src/agents/prompts/architect.md              "\[new\] PREFIX RULE"
  check "v0.33 A3 anchor"        src/agents/prompts/architect.md              "NO INVESTIGATION NOTES"
}

preflight_v034() {
  check "v0.34 B1 allowlist"     src/qa/hallucination_guard.py    "HALLUCINATION_ALLOWLISTS"
  check "v0.34 B1 downgrade op"  src/qa/hallucination_guard.py    "hallucination_finding_downgraded"
  check "v0.34 B2 helper"        src/orchestrator/worktree.py     "_sibling_header_paths"
  check "v0.34 B2 config"        src/config/schema.py             "include_headers_for_sparse"
  check "v0.34 B3 helper"        src/orchestrator/drift_verifier.py "_patch_similarity"
  check "v0.34 B3 ledger op"     src/state/ledger.py              "drift_convergence_failure"
}

preflight_v035() {
  check "v0.35 C1 field"          src/state/knowledge.py  "quarantined:"
  check "v0.35 C1 audit file"     src/state/knowledge.py  "quarantine_audit.jsonl"
  check "v0.35 C1 evaluator"      src/state/knowledge.py  "_evaluate_quarantine"
  check "v0.35 C2 gate"           src/state/knowledge.py  "_critic_evidence_quality"
  check "v0.35 C3 decay curve"    src/state/knowledge.py  "confirmations_7d"
  check "v0.35 C1 quarantine op"  src/state/ledger.py     "knowledge_entry_quarantined"
  check "v0.35 C1 credit op"      src/state/ledger.py     "knowledge_lesson_credited"
}

preflight_v036() {
  check "v0.36 F1 recovery op"        src/state/ledger.py                          "recovery_tier_attempted"
  check "v0.36 F1 attempt op"         src/state/ledger.py                          "architect_attempt_failed"
  check "v0.36 F2 exception"          src/adapters/base.py                         "NetworkProbeFailure"
  check "v0.36 G1 module"             src/orchestrator/spec_validator.py           "validate_spec_text"
  check "v0.36 E1 default factory"    src/config/schema.py                         "huge_repo_multipliers: dict\[str, float\] = Field"
  check "v0.36 D1 templates"          src/orchestrator/retry_envelope.py           "_DIAGNOSIS_TEMPLATES"
  check "v0.36 D3 helper"             src/orchestrator/plan_phase_recovery.py      "should_change_model_for_class"
}

preflight_v037() {
  check "v0.37 H1 helper"             src/orchestrator/execute_phase.py            "_build_recent_evidence_block"
  check "v0.37 H1 header"             src/orchestrator/execute_phase.py            "REVIEWER_RAW:"
  check "v0.37 H1 config"             src/config/schema.py                         "recent_evidence_max_chars_per_kind"
  check "v0.37 H2 parser cap"         src/orchestrator/corrective_parser.py        "max_tasks"
  check "v0.37 H2 ledger op"          src/state/ledger.py                          "corrective_cap_reached"
  check "v0.37 H2 config"             src/config/schema.py                         "max_corrective_tasks_per_phase"
  check "v0.37 H2 status literal"     src/state/schemas.py                         "\"capped\""
  check "v0.37 H3 method"             src/orchestrator/circuit_breaker.py          "record_test_diagnosis"
  check "v0.37 H3 reset_adapter"      src/orchestrator/circuit_breaker.py          "reset_adapter"
  check "v0.37 H3 config"             src/config/schema.py                         "test_diag_breaker_threshold"
  check "v0.37 H4 helper"             src/adapters/detect.py                       "_detect_trigger_context"
  check "v0.37 H4 config"             src/config/schema.py                         "adapter_respect_trigger_context"
  check "v0.37 H5 module"             src/orchestrator/repo_size.py                "def is_huge_repo"
  check "v0.37 H5 resolver"           src/orchestrator/huge_repo_overrides.py      "resolve_huge_repo_value"
  check "v0.37 H5 hallucination"      src/qa/hallucination_guard.py                "huge_repo_cpp_paths_included"
  check "v0.37 H5 master switch"      src/config/schema.py                         "huge_repo_overrides_disabled"
}

case "$target" in
  v0.32) preflight_v032 ;;
  v0.33) preflight_v033 ;;
  v0.34) preflight_v034 ;;
  v0.35) preflight_v035 ;;
  v0.36) preflight_v036 ;;
  v0.37) preflight_v037 ;;
  all)   preflight_v032 ; preflight_v033 ; preflight_v034 ; preflight_v035 ; preflight_v036 ; preflight_v037 ;;
  *)
    echo "Unknown target: $target (expected v0.32 | v0.33 | v0.34 | v0.35 | v0.36 | v0.37 | all)" >&2
    exit 2
    ;;
esac

echo "All preflight greps passed for: $target"
