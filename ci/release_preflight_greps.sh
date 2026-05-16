#!/usr/bin/env bash
# Mirror of the grep-style preflights in .github/workflows/release.yml so
# the same checks can be run locally before pushing a release tag.
#
# Usage: ci/release_preflight_greps.sh [v0.32 | v0.33 | v0.34 | v0.35 | all]   (default: all)

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
  check "v0.35 C1 field"         src/state/knowledge.py "quarantined:"
  check "v0.35 C1 audit file"    src/state/knowledge.py "quarantine_audit.jsonl"
  check "v0.35 C1 evaluator"     src/state/knowledge.py "_evaluate_quarantine"
  check "v0.35 C2 gate"          src/state/knowledge.py "_critic_evidence_quality"
  check "v0.35 C3 decay curve"   src/state/knowledge.py "confirmations_7d"
  check "v0.35 C1 quarantine op" src/state/ledger.py    "knowledge_entry_quarantined"
  check "v0.35 C1 credit op"     src/state/ledger.py    "knowledge_lesson_credited"
}

case "$target" in
  v0.32) preflight_v032 ;;
  v0.33) preflight_v033 ;;
  v0.34) preflight_v034 ;;
  v0.35) preflight_v035 ;;
  all)   preflight_v032 ; preflight_v033 ; preflight_v034 ; preflight_v035 ;;
  *)     echo "Unknown target: $target (try: v0.32 | v0.33 | v0.34 | v0.35 | all)"; exit 2 ;;
esac

echo "All preflight greps passed for: $target"
