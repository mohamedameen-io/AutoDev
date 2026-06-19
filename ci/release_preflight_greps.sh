#!/usr/bin/env bash
# Mirror of the grep-style preflights in .github/workflows/release.yml so
# the same checks can be run locally before pushing a release tag.
#
# Usage: ci/release_preflight_greps.sh [v0.32 | v0.33 | ... | v1.0.0 | all]
#        (default: all)
#
# Each pre-v1.0 block locks the integration of a shipped phase against
# accidental removal via cheap presence greps — a first tripwire only.
#
# The v1.0.0 gate (Phase 0 / B6, closes WS3-release-gate-presence-only-anchors)
# is DIFFERENT: ``preflight_v100`` is BEHAVIORAL. It runs the resolver
# engagement suite (``pytest -m resolver_enabled``) and FAILS unless the
# engagement op demonstrably FIRES (>0 passed/xfailed AND >0 collected). A
# presence grep can pass on dead code; this one cannot.
#
# The N2 broken-control (must turn this RED):
#   AUTODEV_RESOLVER_FORCE_DISABLED=1 bash ci/release_preflight_greps.sh v1.0.0
# ``AUTODEV_RESOLVER_FORCE_DISABLED`` is the OPERATOR override that the
# tests/conftest.py autouse fixture does NOT auto-unset (unlike the suite's
# internal default-off ``AUTODEV_RESOLVER_DISABLED``, which conftest manages),
# so it forces the resolver OFF for the whole engagement suite, the engagement
# assertions go red, pytest exits non-zero, and this script exits non-zero.

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

preflight_v038() {
  check "v0.38 I1 enforcer cwd"       src/guardrails/enforcer.py                   "_eff_max_duration_s"
  check "v0.38 I1 enforcer log"       src/guardrails/enforcer.py                   "huge_repo_caps_applied"
  check "v0.38 I1 ttl helper"         src/orchestrator/repo_size.py                "is_huge_repo_with_ttl"
  check "v0.38 I1 lang threshold"     src/config/schema.py                         "huge_cpp_lang_threshold"
  check "v0.38 I2 capped-phases"      src/cli/commands/requeue.py                  "capped-phases"
  check "v0.38 I2 capped panel"       src/cli/commands/status.py                   "_render_capped_phases_panel"
  check "v0.38 I2 count helper"       src/cli/commands/status.py                   "_count_ops_by_name"
  check "v0.38 I2 audit op"           src/state/ledger.py                          "capped_phases_selected"
  check "v0.38 I3 plan cap config"    src/config/schema.py                         "max_corrective_tasks_per_plan"
  check "v0.38 I3 scope field"        src/state/plan_manager.py                    "scope.*plan"
  check "v0.38 I3 skip loop op"       src/state/ledger.py                          "skip_corrective_loop_detected"
  check "v0.38 I3 phase metadata"     src/state/schemas.py                         "Phase.metadata\|metadata: dict"
  check "v0.38 I4 backoff method"     src/orchestrator/circuit_breaker.py          "next_backoff_s_for_test_diag"
  check "v0.38 I4 success method"     src/orchestrator/circuit_breaker.py          "record_test_success"
  check "v0.38 I4 budget method"      src/orchestrator/circuit_breaker.py          "test_diag_budget_exhausted"
  check "v0.38 I4 backoff config"     src/config/schema.py                         "test_diag_backoff_total_budget_s"
  check "v0.38 I4 typed halt"         src/tournament/errors.py                     "halted_task_id"
  check "v0.38 I4 drain timeout"     src/config/schema.py                         "parallel_pool_drain_timeout_s"
  check "v0.38 I5 envelope dump"      src/orchestrator/execute_phase.py            "_dump_architect_consult_envelope"
  check "v0.38 I5 developer label"    src/orchestrator/execute_phase.py            "DEVELOPER_RAW"
  check "v0.38 I5 cursor allowlist"   src/adapters/detect.py                       "_CURSOR_ENV_ALLOWLIST"
  check "v0.38 I5 tmux warn"          src/adapters/detect.py                       "tmux_screen_detected"
  check "v0.38 I5 adapter_selected"   src/state/ledger.py                          "adapter_selected"
  check "v0.38 I5 selection source"   src/adapters/detect.py                       "_classify_selection_source"
}

preflight_v039() {
  check "v0.39 J1 cost recorder"      src/orchestrator/cost_recorder.py            "class CostRecordingAdapter"
  check "v0.39 J1 run summary"        src/state/run_summary.py                     "def append_run_summary"
  check "v0.39 J1 invocation op"      src/state/ledger.py                          "invocation_cost"
  check "v0.39 J2 profile module"     src/orchestrator/huge_repo_profile.py        "def apply_huge_repo_profile"
  check "v0.39 J2 profile sparse"     src/orchestrator/huge_repo_profile.py        "worktree_sparse_checkout_enabled"
  check "v0.39 J2 unbuildable"        src/qa/detect.py                             "def is_repo_unbuildable"
  check "v0.39 J2 probe model"        src/config/schema.py                         "probe_model"
  check "v0.39 J2 suppress cfg"       src/config/schema.py                         "suppress_target_repo_config"
  check "v0.39 J2 budget ceilings"    src/config/schema.py                         "class BudgetEscalationConfig"
  check "v0.39 J3 parallelism"        src/orchestrator/huge_repo_overrides.py      "def resolve_huge_repo_parallelism"
  check "v0.39 J3 worktree cone"      src/orchestrator/worktree.py                 "default_sparse_paths"
  check "v0.39 J3 stale lock"         src/adapters/git_utils.py                    "def clear_stale_index_lock"
  check "v0.39 J4 accept helper"      src/orchestrator/execute_phase.py            "_maybe_accept_approved_on_exhaustion"
  check "v0.39 J4 accept op"          src/state/ledger.py                          "accepted_approved_on_exhaustion"
  check "v0.39 J5 containment"        src/orchestrator/execute_phase.py            "_diff_confined_to_autodev"
  check "v0.39 J5 containment op"     src/state/ledger.py                          "containment_violation_autodev_paths"
  check "v0.39 J6 drift partition"    src/orchestrator/drift_verifier.py           "def partition_drift_findings"
}

preflight_v040() {
  check "v0.40 framing phase"         src/orchestrator/framing_phase.py     "async def run_framing_phase"
  check "v0.40 specialist dispatch"   src/orchestrator/framing_phase.py     "_invoke_framing_role"
  check "v0.40 altitude panel"        src/orchestrator/framing_phase.py     "_run_altitude_judge_panel"
  check "v0.40 local shuffle"         src/orchestrator/framing_phase.py     "def _shuffle_approaches"
  check "v0.40 recurrence signal"     src/orchestrator/framing_signals.py   "def compute_recurrence_at_seam"
  check "v0.40 boundary signal"       src/orchestrator/framing_signals.py   "def compute_boundary_repeatedly_touched"
  check "v0.40 evidence schema"       src/state/schemas.py                  "class FramingEvidence"
  check "v0.40 approach schema"       src/state/schemas.py                  "class SolutionApproach"
  check "v0.40 phase config"          src/config/schema.py                  "class FramingPhaseConfig"
  check "v0.40 denylist role"         src/config/schema.py                  "altitude_judge"
  check "v0.40 classified op"         src/state/ledger.py                   "framing_classified"
  check "v0.40 strategy op"           src/state/ledger.py                   "framing_strategy_chosen"
  check "v0.40 call site"             src/orchestrator/plan_phase.py        "run_framing_phase"
  check "v0.40 architect coupling"    src/agents/prompts/architect.md       "FRAMING PHASE COUPLING"
  check "v0.40 framing prompt"        src/agents/prompts/framing.md         "CLASSIFICATION:"
  check "v0.40 judge prompt"          src/agents/prompts/altitude_judge.md  "RANKING:"
}

preflight_v041() {
  # A1 — reviewer turn-exhaustion soft-pass (not a developer discard)
  check "v0.41 A1 reviewer infra"     src/orchestrator/execute_phase.py            "_reviewer_exhausted_turns"
  check "v0.41 A1 softpass marker"    src/orchestrator/execute_phase.py            "reviewer_infra_softpass"
  # A2 — depends_on emission + inference + plan-gate warning
  check "v0.41 A2 inference"          src/orchestrator/dependency_inference.py     "def infer_plan_dependencies"
  check "v0.41 A2 dag warning"        src/orchestrator/dag.py                      "def warn_unordered_file_sharers"
  check "v0.41 A2 architect rule"     src/agents/prompts/architect.md              "TASK DEPENDENCIES"
  # A3 — merge-rollback on failed 3-way
  check "v0.41 A3 abort helper"       src/orchestrator/worktree.py                 "def abort_failed_apply"
  # A4 — text-only tournament roles drop Read
  check "v0.41 A4 no-tool roles"      src/tournament/llm.py                        "_TEXT_ONLY_NO_TOOL_ROLES"
  # Intake phase (ADR-0045)
  check "v0.41 intake phase"          src/orchestrator/intake_phase.py             "async def run_intake_phase"
  check "v0.41 intake gather"         src/orchestrator/intake_sources/__init__.py  "async def gather_facts"
  check "v0.41 intake assess"         src/orchestrator/spec_validator.py           "def assess"
  check "v0.41 intake config"         src/config/schema.py                         "class IntakePhaseConfig"
  check "v0.41 intake evidence"       src/state/schemas.py                         "class IntakeEvidence"
  check "v0.41 intake ledger op"      src/state/ledger.py                          "spec_locked"
  check "v0.41 intake enricher"       src/agents/prompts/intake_enricher.md        "INTAKE ENRICHER"
  check "v0.41 intake clarifier"      src/agents/prompts/intake_clarifier.md       "CONSTRAINTS, NOT SOLUTIONS"
  check "v0.41 intake call site"      src/orchestrator/plan_phase.py               "run_intake_phase"
  # Diagnosis phase (ADR-0046)
  check "v0.41 diagnosis phase"       src/orchestrator/diagnosis_phase.py          "async def run_diagnosis_phase"
  check "v0.41 diagnosis prompt"      src/agents/prompts/diagnostician.md          "SANDBOX"
  check "v0.41 diagnosis config"      src/config/schema.py                         "class DiagnosisPhaseConfig"
  check "v0.41 diagnosis evidence"    src/state/schemas.py                         "class DiagnosisEvidence"
  check "v0.41 diagnosis ledger op"   src/state/ledger.py                          "seam_finding"
  check "v0.41 reproduce gate"        src/qa/reproduce_gate.py                     "async def run_reproduce_gate"
  check "v0.41 debug-tag gate"        src/qa/debug_tag_gate.py                     "async def run_debug_tag_gate"
  check "v0.41 diagnosis call site"   src/orchestrator/plan_phase.py               "run_diagnosis_phase"
  check "v0.41 framing diag signal"   src/orchestrator/framing_phase.py            "diagnosis_signals"
}

preflight_v042() {
  # ADR-0047 — Universal Blocker Resolver
  check "v0.42 resolver module"       src/orchestrator/blocker_resolver.py         "async def resolve_blocker"
  check "v0.42 resolver fastpath"     src/orchestrator/blocker_resolver.py         "def deterministic_action"
  check "v0.42 resolver degrade"      src/orchestrator/blocker_resolver.py         "async def record_phase_degrade"
  check "v0.42 resolver prompt"       src/agents/prompts/resolver.md               "You are the RESOLVER agent"
  check "v0.42 failure classes"       src/orchestrator/failure_classes.py          "STRUCTURAL_FAILURE_CLASSES"
  check "v0.42 resolver chokepoint"   src/orchestrator/execute_phase.py            "_maybe_resolve_blocker"
  check "v0.42 resolver config"       src/config/schema.py                         "class ResolverConfig"
  check "v0.42 resolver role"         src/config/schema.py                         "SPECIALIST_ROLES"
  check "v0.42 blocker context"       src/state/schemas.py                         "class BlockerContext"
  check "v0.42 resolution action"     src/state/schemas.py                         "class ResolutionAction"
  check "v0.42 resolver ledger op"    src/state/ledger.py                          "resolution_chosen"
  check "v0.42 resolver killswitch"   src/orchestrator/execute_phase.py            "AUTODEV_RESOLVER_DISABLED"
  # C1 — intake/diagnosis DOA fix (specialist-role backfill) + source gates
  check "v0.42 C1 backfill"           src/config/loader.py                         "_backfill_specialist_roles"
  check "v0.42 C1 github gate"        src/orchestrator/intake_sources/github.py    "_gh_available"
  # A4 — plan-tournament text-only tool scoping
  check "v0.42 A4 plan-tourn scope"   src/orchestrator/plan_tournament_runner.py   "_TEXT_ONLY_NO_TOOL_ROLES"
  # C3 — cross-phase depends_on validation
  check "v0.42 C3 undefined refs"     src/orchestrator/dag.py                      "def validate_dag_undefined_refs"
  check "v0.42 C3 global cycles"      src/orchestrator/dag.py                      "def validate_dag_cycles_global"
  # C4 — worktree pool atomicity
  check "v0.42 C4 claim lock"         src/orchestrator/worktree_pool.py            "_claim_lock"
  check "v0.42 C4 task index"         src/orchestrator/worktree_pool.py            "_task_to_path"
  # C5 — corrective test-repair duration cap
  check "v0.42 C5 cap field"          src/config/schema.py                         "max_duration_s_per_test_repair_task"
  check "v0.42 C5 enforcer select"    src/guardrails/enforcer.py                   "_eff_max_duration_s_per_test_repair_task"
}

preflight_v042_1() {
  # v0.42.1 — make v0.42.0 features actually engage (Run-5 gate fixes)
  # F1 — universal resolver escalation by construction
  check "v0.42.1 F1 block_task"       src/orchestrator/blocker_guard.py            "async def block_task"
  check "v0.42.1 F1 single degrade"   src/orchestrator/blocker_guard.py            "record_phase_degrade"
  check "v0.42.1 F1 block_task used"  src/orchestrator/execute_phase.py            "block_task"
  check "v0.42.1 F1 hook register"    src/orchestrator/execute_phase.py            "block_hook"
  check "v0.42.1 F1d enforcement"     tests/test_block_path_invariant.py           "only_block_task_commits_blocked"
  check "v0.42.1 F1 framing degrade"  src/orchestrator/framing_phase.py            "record_phase_degrade"
  check "v0.42.1 F1 tourn degrade"    src/orchestrator/plan_phase.py               "record_phase_degrade"
  # F2 — A4: bounded tournament inputs + real tool-scoping
  check "v0.42.1 F2 _limit hoist"     src/tournament/util.py                       "def _limit"
  check "v0.42.1 F2a plan bound"      src/tournament/plan_tournament.py            "_limit("
  check "v0.42.1 F2b adapter scope"   src/adapters/claude_code.py                  "inv.allowed_tools is not None"
  # F3 — intake gather: repo activation + autonomous github discovery
  check "v0.42.1 F3 repo skip log"    src/orchestrator/intake_sources/repo.py      "repo_skipped"
  check "v0.42.1 F3 gh discovery"     src/orchestrator/intake_sources/github.py    "_gh_issue_list"
  check "v0.42.1 F3 gh match guard"   src/orchestrator/intake_sources/github.py    "_best_match"
  # F4 — diagnosis: richer context + always-emit-loop mandate
  check "v0.42.1 F4 struct findings"  src/orchestrator/diagnosis_phase.py          "files_referenced"
  check "v0.42.1 F4 short-resp warn"  src/orchestrator/diagnosis_phase.py          "suspiciously_short_response"
  check "v0.42.1 F4 loop mandate"     src/agents/prompts/diagnostician.md          "Never emit nothing"
  # F5 — engagement tests
  check "v0.42.1 F5 engagement"       tests/test_resolver_engagement.py            "assert_no_silent_dead_ends"
}

preflight_v043() {
  # v0.43.0 — Phase-4 stabilization: the WS-4 binding constraint resolved + the
  # field findings (F-1..F-7). Cheap presence tripwires locking each fix's
  # integration anchor against accidental removal (a first tripwire only).
  # F-1 — plan-repair admits a task's own declared files into its edit_scope
  check "v0.43 F-1 scope repair"      src/orchestrator/plan_phase.py               "repair_phase_edit_scope"
  # F-2 — phase-scoped corrective non-convergence ceiling (fail-loud, not churn)
  check "v0.43 F-2 ceiling cfg"       src/config/schema.py                         "max_corrective_cycles_per_phase"
  check "v0.43 F-2 ceiling op"        src/state/ledger.py                          "corrective_nonconvergent_ceiling"
  # F-4 — apply-time edit-scope enforcement (warn-default) + binary-aware diff
  check "v0.43 F-4 apply-scope cfg"   src/config/schema.py                         "enforce_apply_time_edit_scope"
  check "v0.43 F-4 apply-scope op"    src/state/ledger.py                          "edit_scope_apply_violation"
  # F-5 — self-contained binary diffs + generated-cruft exclusion (the .pyc fix)
  check "v0.43 F-5 diff filter"       src/adapters/git_utils.py                    "filter_generated_from_diff"
  # F-6 — per-task worktree includes a capped, scoped test-harness globset
  check "v0.43 F-6 harness cone cfg"  src/config/schema.py                         "worktree_sparse_include_harness"
  # F-7 — default-off plan-phase wall-clock ceiling (salvage, not opaque SIGKILL)
  check "v0.43 F-7 plan wall cfg"     src/config/schema.py                         "plan_phase_wall_budget_s"
  check "v0.43 F-7 plan wall op"      src/state/ledger.py                          "plan_phase_wall_budget_exceeded"
}

# ---------------------------------------------------------------------------
# v1.0.0 — the BEHAVIORAL engagement gate (Phase 0 / B6, gate N2).
#
# Replaces the pure-grep "resolver-engagement" anchors (kept above as a cheap
# first tripwire) with a real run of the engagement suite. This is the gate the
# v0.42.x grep anchors could not be: presence ≠ engagement.
# ---------------------------------------------------------------------------
preflight_v100() {
  # 1) Cheap tripwire first: the resolver + engagement-test anchors must exist.
  #    (Behavioral gate below is the real authority; these just fail fast/clear.)
  check "v1.0 resolver chokepoint"   src/orchestrator/execute_phase.py  "_maybe_resolve_blocker"
  check "v1.0 single block setter"   src/orchestrator/blocker_guard.py  "async def block_task"
  check "v1.0 engagement suite"      tests/test_resolver_engagement.py  "assert_no_silent_dead_ends"

  # 2) BEHAVIORAL: run the engagement suite (resolver ON for the real gate).
  echo ">>>> [v1.0] running engagement suite: uv run pytest -m resolver_enabled -q"
  local out_file rc
  out_file="$(mktemp)"
  # Do NOT pipe pytest into tail/grep — a pipe masks pytest's exit code. Capture
  # output to a file, capture the exit code directly, then parse the file. The
  # ``|| true`` keeps ``set -e`` from aborting before we can inspect ``rc``.
  uv run pytest -m resolver_enabled -q >"$out_file" 2>&1 && rc=0 || rc=$?
  echo "---- engagement suite tail ----"
  tail -n 8 "$out_file"
  echo "-------------------------------"

  # 2a) Non-zero exit is a hard fail (the broken-control path lands here).
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [v1.0 engagement]: pytest -m resolver_enabled exited non-zero (rc=$rc)"
    rm -f "$out_file"
    return 1
  fi

  # 2b) Zero-collected is a hard fail — an empty marker set must NOT pass
  #     vacuously (the exact anti-vacuity property the gate requires).
  if grep -qiE 'collected 0 items|no tests ran' "$out_file"; then
    echo "FAIL [v1.0 engagement]: 0 tests collected for marker resolver_enabled (vacuous pass blocked)"
    rm -f "$out_file"
    return 1
  fi

  # 2c) Engagement op must demonstrably FIRE: the summary must report >0
  #     passed OR >0 xfailed for the marker (a brand-new xfail realloop test
  #     still counts as the suite running, per the B6 note). Parse the pytest
  #     summary line ("N passed", "N xfailed").
  local n_passed n_xfailed
  n_passed="$(grep -oE '[0-9]+ passed' "$out_file" | grep -oE '[0-9]+' | tail -n 1 || true)"
  n_xfailed="$(grep -oE '[0-9]+ xfailed' "$out_file" | grep -oE '[0-9]+' | tail -n 1 || true)"
  n_passed="${n_passed:-0}"
  n_xfailed="${n_xfailed:-0}"
  if [ "$n_passed" -eq 0 ] && [ "$n_xfailed" -eq 0 ]; then
    echo "FAIL [v1.0 engagement]: suite ran but reported 0 passed AND 0 xfailed — engagement op did not fire"
    rm -f "$out_file"
    return 1
  fi

  rm -f "$out_file"
  echo "ok   [v1.0 engagement]: resolver engagement fired ($n_passed passed, $n_xfailed xfailed, >0 collected)"
}

case "$target" in
  v0.32) preflight_v032 ;;
  v0.33) preflight_v033 ;;
  v0.34) preflight_v034 ;;
  v0.35) preflight_v035 ;;
  v0.36) preflight_v036 ;;
  v0.37) preflight_v037 ;;
  v0.38) preflight_v038 ;;
  v0.39) preflight_v039 ;;
  v0.40) preflight_v040 ;;
  v0.41) preflight_v041 ;;
  v0.42) preflight_v042 ;;
  v0.42.1) preflight_v042_1 ;;
  # v0.43.0 — Phase-4 stabilization. release.yml passes the bare X.Y.Z version,
  # so match both the bare and v-prefixed forms. Runs only this release's block
  # (per the per-version pattern); the engagement suite is a separate release step.
  0.43.0|v0.43.0|v0.43) preflight_v043 ;;
  # The v1.0 release gate. ``v1.0.0``/``v1.0`` AND any plain ``1.0.0``-style
  # input (release.yml passes the bare X.Y.Z version) run the BEHAVIORAL
  # engagement gate on top of every cheap presence tripwire.
  v1.0.0|v1.0|1.0.0|1.0)
    preflight_v032 ; preflight_v033 ; preflight_v034 ; preflight_v035 ; preflight_v036 ; preflight_v037 ; preflight_v038 ; preflight_v039 ; preflight_v040 ; preflight_v041 ; preflight_v042 ; preflight_v042_1 ; preflight_v100 ;;
  all)   preflight_v032 ; preflight_v033 ; preflight_v034 ; preflight_v035 ; preflight_v036 ; preflight_v037 ; preflight_v038 ; preflight_v039 ; preflight_v040 ; preflight_v041 ; preflight_v042 ; preflight_v042_1 ; preflight_v100 ;;
  *)
    echo "Unknown target: $target (expected v0.32 | v0.33 | v0.34 | v0.35 | v0.36 | v0.37 | v0.38 | v0.39 | v0.40 | v0.41 | v0.42 | v0.42.1 | v0.43.0 | v1.0.0 | all)" >&2
    exit 2
    ;;
esac

echo "All preflight greps passed for: $target"
