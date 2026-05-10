# Changelog

All notable changes to AutoDev. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning per [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - v0.23.0 in progress

### Added
- **C5 — Explorer max_turns 2x on huge repos.** The explorer's job is to enumerate the codebase; on huge repos (Unity: 358K files) the default 3 turns is insufficient. P-7's investigation of the 2026-05-09 Unity `.autodev/debug/` showed the explorer hit `error_max_turns` at turn 3 with 218K cached tokens. Wired in `orchestrator/plan_phase.py:_delegate` against `orch._repo_capacity.is_huge`. Other roles unaffected.

### Remaining for v0.23.0
- C1 (`WorktreeConfig` huge_repo_mode + sparse-by-default + async removal)
- C2 (secretscan diff-mode default, entropy bump, ignore_paths)
- C3 (signal handlers + child-kill on cancel + lockfile PID)
- C4 (plan-tournament huge fast-path: single-branch + reduced passes)
- C6 (`regex_timeout` ledger op telemetry, building on v0.22.1 A1)
- C7 (huge_repo_guide.md + ADR)

### Remaining for v0.22.2
- B3 (atomic evidence/ledger via `attempt_started` marker + `reconcile_evidence_vs_ledger`)
- B4 (full path normalization pipeline + architect-retry envelope)

## [0.22.2] - 2026-05-10

### Fixed (FSM resilience)
- **B1 — Resume reaper for orphan in-flight tasks.** New `PlanManager.reap_orphans()` walks the plan and reverts every task in `{in_progress, coded, auto_gated, reviewed, tested, tournamented}` back to `pending` via the existing `revert_task_to_pending` primitive. Wired into `run_execute_phase` before any dispatch — interrupted runs now self-heal on `autodev resume`. Idempotent. Resolves D-2's finding from the 2026-05-09 Unity stall (4 tasks frozen between `coded` and `complete`, unrecoverable without manual ledger surgery).
- **B2 — `PhaseStuckError` replaces silent FSM stall.** `_execute_phase_dag` previously returned silently when a phase had zero pending but tasks remained non-terminal — runs looked like clean completions while data was wedged. Now raises `errors.PhaseStuckError(phase_id, stuck_task_ids)` so operators see the offending tasks and the recovery hint (`autodev resume`).

### Added
- `errors.PhaseStuckError` (subclass of `AutodevError`) with `phase_id` and `stuck_task_ids` fields.
- `PlanManager.reap_orphans()` — orphan in-flight task sweeper.

### Deferred to v0.23.0
- Atomic evidence ↔ ledger barrier (`attempt_started` marker + `reconcile_evidence_vs_ledger`) — D-3's finding from the Unity stall, requires more design surface than a patch.
- Full path normalization pipeline (`path_validator.normalize_path` + architect-retry envelope) — substantial new module.

Roadmap: `thoughts/shared/plans/2026-05-10-huge-repo-stability-roadmap.md`.

## [0.22.1] - 2026-05-10

### Fixed (huge-repo crash patches)
- **A1 — `qa.cpp_symbols` regex linearization + `hallucination_guard` watchdog.** Replaces the multi-line `_DECL` pattern (nested unbounded quantifier susceptible to catastrophic backtracking on Unity-scale C++ headers) with a per-line `_DECL_LINE` scan. Wraps `_dispatch` in `asyncio.wait_for(asyncio.to_thread(...))` with a per-file timeout (default 10 s, configurable via `qa_gates.regex_timeout_per_file_s`); on timeout the file is skip-and-warn'd. Resolves the 2026-05-09 Unity stall (40+ min CPU pin in `_sre_SRE_Pattern_findall`).
- **A2 — `secretscan` auto-skip on huge repos.** When `runtime.repo_probe.RepoCapacity.is_huge` is `True`, the gate dispatcher disables `secretscan` and surfaces a structured warning. Override per-repo with `qa_gates.secretscan_force_run_on_huge_repo=True`. Avoids the 27K-50K false-positive avalanche observed on Unity (asset GUIDs cleared the 4.5 entropy default). Full FP redesign deferred to v0.23.0.
- **A3 — `WorktreeManager` huge-repo create timeout.** Adds `huge_mode` and `huge_create_timeout_s` (default 600 s) to `WorktreeManager.__init__`. The orchestrator threads `is_huge` from the repo probe so `git worktree add` no longer hits the historical 60 s ceiling on Unity-scale full checkouts (~80-180 s observed).
- **A4 — `EditScopeViolation` surfaces both raw and normalized paths.** Strips surrounding quotes/backticks/`./` and applies `posixpath.normpath` at the raise site so ledger events name the path malformation unambiguously rather than truncating with `…`. Full normalization pipeline (architect-retry, structured errors) lands in v0.22.2.
- **A5 — `_git_diff_with_untracked` captures new files in adapter evidence.** New helper in `adapters.git_utils` mirrors the already-correct `WorktreeManager.get_diff_vs_base` (tracked diff + per-untracked `git diff --no-index` blocks). Switched the `claude_code` adapter call site so developer tasks creating new files (e.g. `notes/*.md`) now produce non-null `evidence.diff` instead of `null`.

### Added
- `QAGatesConfig.regex_timeout_per_file_s: float = 10.0` — per-file watchdog ceiling for content-scanning QA gates.
- `QAGatesConfig.secretscan_auto_skip_huge_repo: bool = True` — auto-skip the secretscan gate on huge repos (default safety valve).
- `QAGatesConfig.secretscan_force_run_on_huge_repo: bool = False` — operator override.

Roadmap and per-item rationale: `thoughts/shared/plans/2026-05-10-huge-repo-stability-roadmap.md`.

## [0.21.1] - 2026-05-09

### Fixed
- Wheel packaging now includes `src/runtime`, which had been silently omitted from the built wheel since v0.10.0. Installs from PyPI no longer fail with `ModuleNotFoundError: runtime` once code paths added in 0.10.0+ (resource probing, parallelism resolution, repo capacity probing) are exercised.

## [0.21.0] - 2026-05-09

### Added
- **Speculative execution** (`src/orchestrator/speculative.py`): child tasks may begin before their parent task completes. A rollback handler resets the worktree on parent failure and emits a `speculative_rolled_back` ledger op.
- **Cross-phase parallelism dispatcher** in `src/orchestrator/execute_phase.py` enables overlapping work across phase boundaries, with `Phase.end_checkpoint_commit` capturing a stable handoff point.
- **WorktreePool warm-start** (`src/orchestrator/worktree_pool.py`): pre-provisioned worktrees in `.autodev/execute_worktrees_pool/` shorten per-task setup latency.
- **Multi-branch impl tournament** (`run_multi_branch_impl_tournament` in `src/orchestrator/impl_tournament_runner.py`): heterogeneous-model fan-out with diff-based meta-merge synthesis, plus a `render_for_diff_synthesis` helper on `ImplContentHandler`.
- New ledger ops and config flags wired through `Phase.end_checkpoint_commit` and the v0.21.0 state schema.

## [0.20.0] - 2026-05-08

### Added
- **LLM PRM (Process Reward Model)**: `cfg.prm.strategy=rules+ml` augments the rules-based trajectory classifier with `LLMTrajectoryClassifier`, configured via the new `PRMConfig`.
- **Regression-based plateau detector**: `cfg.plateau_detector.strategy=regression` enables a pure-Python OLS detector behind the new `PlateauDetectorConfig`; cross-family detection mode is also added.
- **Mutation-test gate**: opt-in `mutation_test_enabled` runs `mutmut` on developer diffs as a promotion gate (Stage 0; equivalence filtering follows in 0.19.0 features extended here).
- **Extended-scope editor expansion**: `Task.extended_scope` lets a task widen its allowed edit set when justified; `extended_scope_critic.py` reviews the expansion, and matching `EXTENDED SCOPE` sections were added to the architect and critic_sounding_board prompts.
- **Dynamic sparse-checkout expansion**: `expand_sparse_paths` and `detect_missing_paths` widen the per-task worktree on missing-file errors instead of failing.
- Per-event-type knowledge decay curves via `DecayCurveConfig`/`KnowledgeConfig.decay_curves`, plus per-bucket huge-repo `max_turns` multipliers (`task_overrides.huge_repo_multipliers`).

## [0.19.0] - 2026-05-08

### Added
- **Mutation-test pipeline** (Stages 1–2): static equivalence filter for surviving mutants and an LLM equivalence judge promote real survivors into a `kill_rate` signal that feeds promotion grading.
- **Holdout-set evaluation** runs before promotion when enabled, gated by a new tournament config toggle.
- **Hallucination guard** extended from Python to TypeScript, JavaScript, and C++.
- **Per-repo secretscan baseline** with a CLI `refresh` subcommand, plus an allowlist and per-extension entropy thresholds.

## [0.18.0] - 2026-05-08

### Added
- **Specialist judge roles**: `judge_roles` and `judge_role_weights` on `TournamentConfig`, with a `JudgeRecusal` module wired into `impl_tournament`.
- **Veto voting strategy**: new `VotingStrategy` protocol with `BordaAggregator` and `VetoAggregator`; selectable via `Tournament.voting_strategy = "veto"`.
- **Cross-family plateau detection** (`cross_family_plateau_enabled`) and a per-family `PlateauDetector` wired into multi-branch dispatch with `force_distant_scout`.
- **Architect council**: prompt + persisted council sidecar; `CriterionVote` schema and `AcceptanceCriterion.vote_history`.
- **Multi-branch phase-review tournament** with majority-vote meta-merge.
- **Lane-aware events**: `TournamentEvent.lane` and lane-tagged lessons threaded through tournament runners.
- **Web search ladder rung**: `execute_phase` wires a `WEB_SEARCH` step with `WEB_CONTEXT` splice into the recovery ladder (the underlying adapter shipped in 0.17.0).

## [0.17.0] - 2026-05-08

### Added
- **WEB_SEARCH escalation rung** between PIVOT and SOFT_BLOCKER, with a 3-search cooldown, a `web_search` adapter (DuckDuckGo HTML scrape default + SerpAPI fallback), and a `WEB CONTEXT MODE` section in the critic_sounding_board prompt. Emits `web_search_invoked` ledger op and threads `StuckState.search_count`.
- **Judge-explorer prompt** with five anti-pattern finding categories; `extract_explorer_findings` and an `ExplorerFinding` dataclass behind `explorer_enabled`.
- **Per-task sparse checkout**: `WorktreeManager.create` accepts `sparse_paths` (cone mode, requires git ≥ 2.25); `execute_phase` forwards phase/plan `edit_scope` as sparse paths.
- **Repeat-hypothesis tagging**: bigram-Jaccard `repeat_detector` flags branches retreading the same hypothesis.
- `Task.files` validator accepts glob patterns; tracked-files cache + glob expansion in `find_file_overlaps`.

### Changed
- `drift_verifier_enabled` default flipped to `True`.

## [0.16.0] - 2026-05-08

### Added
- **Hallucination guard** (`hallucination_guard.py`, Python AST-based) wired into the gate sequence; toggled by `cfg.hallucination_guard` (default `True`).
- **Drift verifier** (`drift_verifier.py`) wired into `phase_review_runner` with a `drift_verifier_complete` ledger op.
- **Promotion ladder**: `PromotionDecision.decide` (with a suspicious-perfect override) integrated into `Tournament.run`; incumbent grade persisted as a sidecar JSON. Gated by `cfg.tournaments.plan.promotion_grade_enabled` (default off).

## [0.15.0] - 2026-05-08

### Added
- **PRM + escalation ladder**: `prm.py` (`TrajectoryEvent` + 5 pattern detectors) is consulted at delegate dispatch, injecting course-corrections; `escalation_ladder.py` introduces `StuckState` and `next_step`. Stuck escalates to a critic via a new `STUCK RECOVERY MODE` prompt section.
- New ledger ops: `stuck_refine`, `stuck_pivot`, `soft_blocker_handoff`. New plan_manager helpers: `increment_discard`, `increment_pivot`, `reset_stuck_state`.
- **Tournament events**: `TournamentEvent` dataclass + `record_tournament_event` helper, emitted from `multi_branch_tournament` meta-merge and `plan_tournament_runner`.

## [0.14.0] - 2026-05-08

### Added
- **`BranchConfig` schema** for multi-branch tournaments: per-branch `model_overrides`, `lane`, `risk`, `family` tags, plus `TournamentPhaseConfig.branches` with validation. Wired through `run_plan_tournament` and multi-branch dispatch.
- **Edit-scope plumbing**: `Plan.edit_scope` and `Phase.edit_scope` schema fields with validators; `validate_edit_scope`/`is_in_scope` helpers in `dag.py`; scope passed into developer prompt injection and `apply_patch_to_main` hunk validation.
- `EDIT_SCOPE` block parsed from plan markdown; `DIRECTIVE PRESERVATION` sections added to architect_b/synthesizer/critic_t prompts; secretscan scoped to `edit_scope` when set.

## [0.13.0] - 2026-05-08

### Added
- **Repo capacity probing**: `runtime.repo_probe` (`RepoCapacity`, `probe_repo`) is invoked once at `Orchestrator.plan()`/`execute()` entry and threaded into `delegate()` `max_turns` resolution via `resolve_task_max_turns(..., capacity=...)`.

### Changed
- `run_secretscan` accepts an optional `paths` parameter; orchestrator passes developer diff paths so scanning is scoped to actual changes.

## [0.12.0] - 2026-05-08

### Added
- **Multi-branch plan tournaments**: N-parallel branch fan-out with deterministic `branch_seed`/`branch_index` namespacing, pairwise meta-merge using the synthesizer, and survivor-floor enforcement (`TournamentError` if under floor). Default `num_branches=3` for plan.
- New ledger ops `multi_branch_plan_tournament_complete` and `meta_merge_complete`; tournament `resume_state` extended for the multi-branch layout; `plan_phase` fallback walks multi-branch artifacts; `latest_incumbent_md_across_branches` helper for salvage.

## [0.11.0] - 2026-05-08

### Added
- **DAG-aware execute_phase**: serial loop replaced with a worker-pool dispatcher driven by topological levels and file-overlap conflict avoidance. Per-task worktree isolation; `WorktreeManager.create_per_task` convenience.
- **Conflict escalation**: `apply_patch_to_main` failures escalate to a critic via a new `CONFLICT ESCALATION MODE` prompt section.
- New helpers: `topological_levels`, `find_blocked_descendants`, `find_file_overlaps`; `plan_manager.next_pending_tasks(limit, exclude_files)`; `mark_blocked_descendants` and in-flight tracking on state.
- New config: `TournamentsConfig.execute_max_parallel_tasks: int | None`.

## [0.10.0] - 2026-05-08

### Added
- **Adaptive subprocess parallelism**: `runtime.resource_probe` (`HostCapacity`, `probe_host`) plus `resolve_parallelism` (role-mix-aware clamping) wired into plan/impl/phase-review runners; per-pass adaptive ratcheting via `maybe_resize_semaphore` and a post-pass RSS probe (`measure_subprocess_rss`).
- `psutil` added as a dependency.

### Changed
- `max_parallel_subprocesses` is now `int | None`, with `None` meaning auto.

### Notes
- This release introduced `src/runtime/`. A packaging bug omitted that directory from the wheel; the fix shipped in v0.21.1.

## [0.9.0] - 2026-05-08

### Added
- **Per-phase code review tournament** (`phase_review_runner`) mirroring the impl tournament; phase-completion detection and tournament invocation wired into `execute_phase`. Default-on `phase_review` `TournamentPhaseConfig`.
- New CLI: `autodev tournament phase-review` for manual re-runs.
- `corrective_parser` for B/AB winner direction text; `PhaseReviewBundle` and `PhaseReviewContentHandler`; `Phase` extended with `acceptance`/`baseline`/`review_status`/`corrective_task_ids`; PlanManager `append_corrective_tasks` and `update_phase_meta`.
- Architect prompt gains `PER-PHASE ACCEPTANCE CRITERIA` directive; markdown parser captures `- Acceptance:` blocks under phase headers.

## [0.8.0] - 2026-05-08

### Added
- **Per-task complexity → max_turns + timeout_s**: `Task.complexity` (Literal) parsed from `- Complexity:` directives; `task_overrides` resolver injects per-task `max_turns` and `timeout_s` into developer invocation; complexity hint injected into the developer envelope. Architect prompt gains `PER-TASK COMPLEXITY` directive.
- `AgentInvocation.timeout_s` field.

## [0.7.0] - 2026-05-07

### Added
- **Complexity-aware judge ensemble**: 7 judges on complex plans (vs. the prior fixed count).

## [0.6.2] - 2026-05-07

### Changed
- `JUDGE_RANK_3_PROMPT` adds a mandatory length-penalty clause with a worked example.
- AB winner is demoted when growth exceeds `max_plan_lines_growth_ratio`.

## [0.6.1] - 2026-05-07

### Added
- `Task.requires` schema field for non-agent-executable tasks; parser captures `Requires:` and `EXECUTABLE_BY:` directives.
- `execute_phase` skips tasks with non-empty `requires`.

### Changed
- Architect prompt tightens `EXECUTION ENVIRONMENT CONSTRAINTS` with the `Requires:` convention.

## [0.6.0] - 2026-05-07

### Added
- **Tournament-failure salvage**: orchestrator recovers latest incumbent from disk on tournament error; `latest_incumbent_md` and `read_incumbent_at` helpers.
- **Winner-stability detector** (`winner_stability_window`); `score_stability_max_delta` bumped.
- New CLI: `autodev tournament promote`.

## [0.5.4] - 2026-05-07

### Added
- `EXECUTION ENVIRONMENT CONSTRAINTS` section in the architect prompt; per-role `role_timeout_s` plumbed through plan/impl/cli tournament runners.

### Fixed
- Expensive-transient retries now capped at 3.

## [0.5.3] - 2026-05-07

### Fixed
- Adapters now dump a debug transcript on timeout, mirroring the rc!=0 path added in 0.5.2.

## [0.5.2] - 2026-05-07

### Fixed
- `claude_code` adapter extracts subtype on the rc!=0 failure path so the deterministic-failure short-circuit fires for `error_max_turns`, `error_max_tokens`, and `error_during_execution` (which surface as rc=1 with JSON in stdout in practice).

## [0.5.1] - 2026-05-07

### Fixed
- `run_plan_tournament` now extracts `plan_complexity` directly from `initial_md` via a new `extract_complexity()` helper, rather than relying on `plan_manager.load()` (which returns `None` during plan_phase because the parsed `Plan` is only persisted after the tournament). Unblocks the per-role `EFFORT_MATRIX` that was silently inert in 0.5.0.

## [0.5.0] - 2026-05-07

### Added
- **Per-role Claude Code `--effort` flag** with agentic plan-complexity classification.
  - Schema: `AgentConfig.effort`, `AutodevConfig.user_complexity`, `Plan.complexity`, `AgentInvocation.effort`.
  - `src/tournament/effort.py` resolver with hardcoded matrix; `claude_code` passes `--effort`; `AdapterLLMClient` honors `role_effort`.
  - Architect emits `COMPLEXITY: simple|medium|complex`; parser captures the line.
  - Wired through `plan_phase`, `execute_phase`, both tournament runners, and the CLI.
  - New `--complexity` CLI flag on `autodev plan`.

## [0.4.1] - 2026-05-07

### Added
- Default-on score-stability runaway detector; default `plan` `num_judges` raised 3 → 5.
- Length-aware judge prompt directive; `no-op is allowed` directives on synthesizer + architect_b prompts (autoreason-derived hardening).

### Fixed
- `effective_winner` is now persisted to `pass_NN/result.json` for observability.

## [0.4.0] - 2026-05-07

### Added
- **Tournament convergence + cost control hardening**.
  - Preamble stripping for synthesizer / architect_b outputs (preventing leakage).
  - Hash short-circuit and score-stability runaway detector.
  - `AgentResult.subtype` surfaced from the claude CLI; deterministic-subtype short-circuit.
  - Per-role `max_turns` and tool restriction in `AdapterLLMClient`.

## [0.3.0] - 2026-05-06

### Added
- **Tournament durability MVP**: resume from on-disk artifacts, tolerate tournament failures, retry on silent claude exits, and dump failure transcripts. Per-role checkpointing within a pass.

## [0.2.0] - 2026-05-06

### Added
- **Initial public release with PyPI packaging.**
- Core orchestrator with `plan_phase` and `execute_phase`.
- Tournament infrastructure (impl tournament, judges, parser, content handlers).
- `claude_code` and `cursor` agent adapters.
- Knowledge ledger with atomic append (reflink-aware clone fallback) and `applied_count` tracking.
- Plugin execution: `QAGatePlugin`, `JudgeProviderPlugin`, `AgentExtensionPlugin`.
- Cost guards: `cost_budget_usd_per_plan`, split `max_tool_calls_per_task`.
- CLI scaffolding includes the `/autodev` slash command and inline-config kickoff rule on `init`.
- npm wrapper package; CI publish workflow with PyPI + npm OIDC trusted publishing.
- Configurable per-role `max_turns`.
- License: GPL-3.0.

### Fixed
- Cursor adapter passes `--force` to bypass the Workspace Trust prompt.
- Tournament correctness: complete rankings, deduplication, narrowed return type.
- Architect no longer writes the plan to a file instead of returning text.

[0.21.1]: #0211---2026-05-09
[0.21.0]: #0210---2026-05-09
[0.20.0]: #0200---2026-05-08
[0.19.0]: #0190---2026-05-08
[0.18.0]: #0180---2026-05-08
[0.17.0]: #0170---2026-05-08
[0.16.0]: #0160---2026-05-08
[0.15.0]: #0150---2026-05-08
[0.14.0]: #0140---2026-05-08
[0.13.0]: #0130---2026-05-08
[0.12.0]: #0120---2026-05-08
[0.11.0]: #0110---2026-05-08
[0.10.0]: #0100---2026-05-08
[0.9.0]: #090---2026-05-08
[0.8.0]: #080---2026-05-08
[0.7.0]: #070---2026-05-07
[0.6.2]: #062---2026-05-07
[0.6.1]: #061---2026-05-07
[0.6.0]: #060---2026-05-07
[0.5.4]: #054---2026-05-07
[0.5.3]: #053---2026-05-07
[0.5.2]: #052---2026-05-07
[0.5.1]: #051---2026-05-07
[0.5.0]: #050---2026-05-07
[0.4.1]: #041---2026-05-07
[0.4.0]: #040---2026-05-07
[0.3.0]: #030---2026-05-06
[0.2.0]: #020---2026-05-06
