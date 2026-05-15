# Changelog

All notable changes to AutoDev. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning per [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - v0.32.0 Phase 2

### Added
- **Review tournament (autoreason A/B/AB pipeline)** —
  `src/orchestrator/review_tournament_runner.py` introduces a
  three-candidate refinement step for the reviewer:
  - **A** is the unchanged developer patch + the original reviewer
    verdict.
  - **B** is an adversarial second-opinion review produced by the
    new `adversarial_reviewer` role
    (`src/agents/prompts/adversarial_reviewer.md`) — deliberately
    framed to find the angle the original reviewer missed.
  - **AB** is a synthesis produced by the new `merge_synthesizer`
    role (`src/agents/prompts/merge_synthesizer.md`) that combines
    A's strengths with B's improvements.
  Three FRESH judges (default cohort:
  `["judge", "minimality_judge", "judge_explorer"]`) blindly score
  the candidates via Borda count. "Do nothing" (A wins
  `convergence_k=2` rounds in a row) is a first-class verdict so
  the loop converges on "the original was fine, stop" instead of
  burning developer-refine cycles.
- New evidence type
  `state.schemas.ReviewTournamentEvidence` carrying tournament_id,
  candidates dict (A / B / AB), judge rankings, Borda scores,
  winner, valid_judges count, converged flag, and rounds; written
  to `.autodev/evidence/{task_id}-review_tournament.json`.
- New ledger ops `review_tournament_started`,
  `review_tournament_judged`, `review_tournament_converged`,
  `review_tournament_escalated` (audit-only — replay is a no-op).
- New config knobs on `TournamentsConfig`:
  `review_tournament_enabled` (default `False`),
  `review_num_judges`, `review_convergence_k`, `review_max_rounds`,
  `review_judge_roles`. Mirror knobs added to the runtime
  `tournament.core.TournamentConfig` dataclass.

### Feature flag rollout (opt-in for v0.32.0)
- The review tournament ships **off by default** for one cycle. To
  opt in, set `cfg.tournaments.review_tournament_enabled = true`
  in `.autodev/config.json` (or programmatically before constructing
  the orchestrator). The legacy single-shot reviewer path is
  byte-identical when the flag is `false`.
- v0.33.0 will flip the default to `true` after one cycle of
  real-world telemetry confirms the do-nothing convergence rate
  matches the autoreason published technique (NousResearch).
- All v0.31.0 instrumentation is preserved by construction:
  - each candidate is grounded against the same chunked review
    envelope (Phase 1.4),
  - each candidate's verdict parses through the existing
    `_parse_review_verdict` so the Phase 1.3 MALFORMED-vs-content
    distinction propagates,
  - `raw_response` is captured on every candidate (Phase 1.2),
  - the empty-result `*-empty.json` dump still fires from the
    adapter layer.

### Tests added
- `tests/test_review_tournament_core.py` — Borda + tiebreak
  invariants, `_no_progress` short-circuit, `_resolve_judge_cohort`
  precedence (14 tests).
- `tests/test_review_tournament_integration.py` — full-flow
  StubAdapter coverage of: B/AB-wins exit semantics, no-progress
  short-circuit (judges never called), max-rounds escalation,
  chunked-envelope reuse across all three candidates, empty-A
  MALFORMED propagation, evidence + ledger breadcrumb writes (6
  tests).

## [0.31.1] - 2026-05-15

Hot-patch fixing the dominant production failure mode that v0.31.0's
own Phase 1.1 instrumentation was supposed to catch but silently
skipped.

### Fixed
- `src/adapters/claude_code.py:352` and `src/adapters/cursor.py:479`:
  the empty-result `*-empty.json` debug dump no longer requires
  `is_error == False`. The v0.31.0 predicate
  `if not is_error and not text.strip(): _dump_empty_result(...)`
  silently skipped the dump whenever the CLI emitted `is_error=true`
  alongside an empty `result` — exactly the transport-layer failure
  shape (timeouts, rate-limited responses with empty bodies, max-
  tokens exhaustion) the dump was built to capture. Empty text is the
  machinery-failure signal; `is_error` is orthogonal to whether we
  should record the forensic dump. The orchestrator still classifies
  `is_error=true` correctly for control flow downstream.

### Tests added
- `tests/test_adapter_empty_result_dump.py::test_claude_empty_result_with_is_error_true_still_dumps`
- `tests/test_adapter_empty_result_dump.py::test_cursor_empty_result_with_is_error_true_still_dumps`
- These would have failed pre-fix; they are the lock that keeps
  Phase 1.1 actually working on the transport-failure path.

### Why this matters
A fresh production run on v0.31.0 surfaced a `MALFORMED` reviewer
verdict (Phase 1.3 working as designed) but produced zero
`*-empty.json` dumps in `.autodev/debug/` — the very forensic
artefact that lets us diagnose the root cause. Without those dumps
the next layer of investigation would have been impossible. This
hot-patch restores the invariant that every empty-result event
leaves a forensic trail.

## [0.31.0] - 2026-05-15

Hardening release. Closes the dominant production failure mode (reviewer
agent emitting empty responses both adapters silently soft-blocked on),
brings the Cursor adapter to parity with the Claude Code adapter on
cancellation / debug-dump / binary-cache discipline, adds a usage-limit-
aware downshift policy for Cursor, introduces a budget-escalation tracker
for repeated `error_max_turns`, and lands the CI infrastructure that
would have caught every v0.30.1 regression at PR time.

### Reviewer pipeline (Phase 1)
- Both adapters now persist a forensic dump to
  `.autodev/debug/<role>-<ts>-empty.json` whenever the underlying CLI
  returns `result == ""` on a clean exit. Previously this case was
  invisible — `returncode == 0` with empty `result` triggered no debug
  artifact, so empty-reviewer failures could not be diagnosed
  post-hoc. Gated behind `AUTODEV_DEBUG_RAW_RESPONSES` (default-on).
- `ReviewEvidence` (and Developer/Test evidence by symmetry) carries a
  new optional `raw_response: str | None` field. The orchestrator writes
  the underlying adapter text even when `output_text` ends up empty, so
  every blocked task is self-diagnosing.
- `_parse_review_verdict()` no longer silently defaults to `APPROVED`
  when no verdict keyword is found. A new `MALFORMED` verdict value
  represents "the reviewer machinery returned something we cannot
  classify" and is treated distinctly from `NEEDS_CHANGES` (legitimate
  negative review).
- Reviewer envelope replaces the hard 8 KB `diff[:8000]` truncation
  with a chunked builder: full diff for files ≤ 2 KB, head+tail+stats
  for larger files, generated/lock files dropped, total soft-capped at
  32 KB. Reviewer `max_turns` bumped 3 → 5. New
  `output_token_budget: int | None` plumbed end-to-end on
  `AgentInvocation` (advisory until adapters expose explicit flags).

### Cursor adapter (Phases 2 + 2.6)
- `CancelledError` is now caught in `execute()` and `healthcheck()`;
  the in-flight subprocess is killed with a 5 s grace before re-raise.
  Mirrors the existing Claude Code adapter discipline. Same handler
  added to `WorktreeManager._run_git()`.
- `_dump_failure_transcript()` helper added; called from every Cursor
  failure path (non-zero exit, parse failure, env-var-skipped
  downshift, downshift-cap-hit, and "no binary available"). First
  rate/usage-limit hit that gets downshifted does NOT dump (the retry
  path is the recovery); only true terminal failures dump.
- `FileNotFoundError` per-binary is now cached within a single
  `execute()` call; we never re-probe the same missing binary across
  model attempts.
- New `max_mode: bool | None` tri-state on `AgentInvocation`. The
  Cursor `_build_command` translates it to CLI flags (currently a
  no-op pending Cursor exposing a public Max Mode flag — see
  `docs/cursor-cli-flags.md`).
- Limit detection expanded beyond rate-limit. New helper
  `_classify_limit_signal()` returns `"rate_limited"` |
  `"usage_limit_hit"` | `"none"` and matches against both stdout AND
  stderr for: `rate limit`, `rate_limit`, `too many requests`,
  `usage limit`, `usage_limit`, `monthly limit`, `plan limit`,
  `quota exceeded`, `out of credits`, `upgrade to continue`,
  `limit reached`. HTTP 429 is still recognised explicitly.
- On any limit hit, every role now downshifts to `model="auto"` with
  `max_mode=False` regardless of the starting model — previously only
  `opus`/`sonnet` starters got a fallback. Capped at one downshift
  per call.
- `usage_limit_hit` added to the v0.30.0 cross-task circuit-breaker
  tracked subtypes alongside `auth_failed`, `rate_limited`,
  `server_error`. Sustained usage-cap hits now pause the phase
  instead of burning escalation budget forever.
- Operator override: `AUTODEV_CURSOR_DISABLE_MAX_FALLBACK=1` skips
  the downshift entirely (for unlimited / enterprise plans).

### Budget escalation (Phase 3)
- New `BudgetEscalationTracker` per orchestrator instance. When the
  same `(task_id, role)` returns `error_max_turns` on consecutive
  attempts:
  - 2nd attempt: `ceil(prior × 1.5)` turns, +25 % timeout.
  - 3rd attempt: `ceil(prior × 2.0)` turns, +50 % timeout. Emits a
    new `budget_escalation` ledger op.
  - 4th attempt: hard fail with `subtype="error_max_turns_escalation_exhausted"`
    and a diagnostic pointing to `.autodev/config.json` overrides.
- Counter resets on success and on any non-`error_max_turns` failure.
- Hard ceilings at `max_turns ≤ 100`, `timeout_s ≤ 3600`
  (overridable via `cfg.budget_escalation`).

### Slash command unification (Phase 4)
- New `src/adapters/slash_command_spec.py` with frozen
  `SlashCommandSpec` dataclass and `canonical_slash_command_spec()`
  builder. Subcommand tuple is derived at module-import time from the
  live `cli.commands` registry.
- `render_claude_slash_command()` and `render_cursor_slash_command()`
  in `src/adapters/inline_config.py` are now thin platform-wrapping
  shims (Claude prepends YAML frontmatter; Cursor passes `--platform
  cursor` everywhere). Body is shared. The two templates cannot drift
  again — three new tests in `tests/test_inline_config.py` lock
  subcommand parity, routing-rule parity, and registry alignment.

### Worktree hygiene + doctor + language fitness (Phase 5)
- `autodev prune` learns `--executor-only` (operates only on
  `.autodev/execute_worktrees/` and `_pool/`) and `--all` (ignores
  age threshold). Combination is the post-SIGKILL emergency cleanup
  path.
- `WorktreeManager` now writes `.autodev/worktrees-state.json` on
  every create / removes the entry on every cleanup. Atomic write.
  Used by `prune --executor-only` and `doctor --repair-worktrees`.
- New `src/runtime/language_profile.py` computes
  `{language: percentage}` over the repo via extension weights.
  Result cached at `.autodev/language_profile.json` with mtime
  invalidation. Recomputed on `init`.
- New `src/adapters/fitness.py` scores adapter fitness against the
  language profile. Cursor scores 95 if TS+JS ≥ 50 %, 80 if ≥ 30 %,
  60 if ≥ 10 %, 30 otherwise. Claude is baseline 85 across languages
  (+5 for Python-heavy projects). Warning printed in `execute` /
  `plan` when score < 50.
- New `AUTODEV_LANG_WEIGHT` env var (default `0.0`, opt-in). When
  > 0, `--platform auto` factors fitness into the choice instead of
  always picking the first-found binary.
- `autodev doctor` gains four new sections: codebase language
  profile (top 5), per-adapter fitness for the selected adapter,
  orphan worktree count (manifest-based), stale editor agent files
  (mtime comparison). New `--repair-worktrees` flag lists orphans
  without deleting.

### CI infrastructure (Phase 6)
- `tests/fixtures/fake_binaries/{fake-claude,fake-cursor}` are pure
  bash mocks that hash the prompt and look up canned responses in
  `$AUTODEV_FAKE_RESPONSE_DIR`. Honour `AUTODEV_FAKE_FAILURE_MODE`
  for `error_max_turns`, `empty_result`, `timeout`, `nonzero_exit`,
  `usage_limit`. macOS + Linux compatible.
- `tests/fixtures/sample_project/` (Python) and
  `tests/fixtures/sample_project_ts/` (TypeScript) feed the E2E
  fixture and the Cursor fitness path.
- `tests/integration/test_e2e_with_fake_binaries.py` — twelve tests
  covering the fake-binary protocol, both adapters end-to-end via
  `PATH`-injected fakes, and explicit regression coverage for
  v0.30.1 Bug F2 (`timeout_s=None`).
- `.github/workflows/test.yml` extended: runs the new E2E suite and
  the slash-template drift gate on every PR.
- `scripts/release_version.py` validates version format,
  `src/_version.py` consistency, and `CHANGELOG.md` entry presence
  before allowing a bump. Refuses to proceed otherwise.
- New `.github/workflows/release.yml` (`workflow_dispatch`):
  preflight-checks → unit-tests → doctor-smoke → manual-smoke-issue
  → tag-and-publish → post-publish-smoke. The existing
  `publish.yml` still handles PyPI + npm; release.yml is the gate
  in front of it.
- New `CONTRIBUTING.md` documents the release ceremony.

### Schema / migration
- `ReviewEvidence`, `DeveloperEvidence`, `TestEvidence` all gain
  optional `raw_response: str | None = None`. Field defaults to
  `None`, so older evidence files continue to load without
  migration. The global `schema_version` is intentionally NOT
  bumped — the new field is additive and backward-compatible.

### Tests
- 2809 passed, 7 skipped (vs 2710 before this release; +99 tests
  across phases).
- New files: `tests/test_orchestrator_review_parser.py`,
  `tests/test_evidence_persists_raw_response.py`,
  `tests/test_adapter_empty_result_dump.py`,
  `tests/test_orchestrator_review_envelope_chunking.py`,
  `tests/test_orchestrator_budget_escalation.py`,
  `tests/test_language_profile.py`,
  `tests/test_adapter_fitness.py`,
  `tests/test_doctor_extended.py`,
  `tests/test_release_version_script.py`,
  `tests/integration/test_e2e_with_fake_binaries.py`.

## [0.30.2] - 2026-05-15

Emergency patch release fixing two regressions in v0.30.1 that broke
end-to-end flows on the Cursor adapter and on Claude Code's
`/autodev` slash command for newer subcommands.

### Fixed
- `src/adapters/cursor.py:143-159`: `CursorAdapter.execute()` no longer
  crashes / misformats the timeout error message when `inv.timeout_s`
  is `None`. Mirrors the `effective_timeout_s` guard the Claude Code
  adapter already had at `src/adapters/claude_code.py:139` (600s
  default). Roles whose per-task complexity overrides leave
  `timeout_s` unset (reviewer, judge, critic_*, etc.) were affected.
- `src/adapters/inline_config.py`: the Claude `/autodev` template
  now lists the `requeue` and `rewind` subcommands. Both were added
  to the CLI registry in v0.28-v0.29 but never propagated to the
  Claude template, so `/autodev requeue` and `/autodev rewind` typed
  inside Claude Code fell into the free-text feature flow (case 4)
  instead of the CLI passthrough (case 2). The Cursor template
  already included them; the two are now in sync. A full
  `SlashCommandSpec` refactor that prevents this class of drift is
  scheduled for v0.31.0.

### Tests added
- `tests/test_adapter_cursor.py::test_execute_default_timeout_when_none`
- `tests/test_adapter_cursor.py::test_execute_default_timeout_message_does_not_say_none`
- `tests/test_inline_config.py::test_render_claude_slash_command_lists_every_cli_subcommand`
  strengthened to assert the full canonical subcommand list (mirroring
  the Cursor equivalent test).

## [0.30.1] - 2026-05-13

Patch release: `autodev init --platform cursor` now installs the
`/autodev` slash command. Cursor 1.6+ supports custom slash commands
at `.cursor/commands/<name>.md` (same shape as Claude Code's
`.claude/commands/<name>.md`), but the v0.30.0 init flow at
`src/cli/commands/init.py:144` explicitly skipped the install for
the `cursor` platform — Cursor users got the background context rules
under `.cursor/rules/<role>.mdc` but no slash entry in the Composer
agent picker. This patch closes that gap.

### Added
- `render_cursor_slash_command()` in `src/adapters/inline_config.py`,
  alongside the existing `render_claude_slash_command()`. Returns a
  Cursor-flavoured passthrough template — plain markdown without
  Claude's `allowed-tools:` / `argument-hint:` frontmatter (Cursor
  command files are reusable prompt templates loaded into the
  Composer agent's input box, not tool-permission scoped). Routes
  to `--platform cursor` everywhere the Claude variant routes to
  `--platform claude_code`. Includes the full v0.29-v0.30 subcommand
  list (notably `requeue` and `rewind`, which the existing Claude
  variant currently omits — to be backfilled in a future release).

### Changed
- `autodev init`'s slash-command install path widened from a single
  conditional `slash_path: Path | None` to a `slash_paths: list[Path]`
  that now writes BOTH files when `--platform auto` (the default):
  - `.claude/commands/autodev.md` for `claude` / `auto`
  - `.cursor/commands/autodev.md` for `cursor` / `auto`
  The pretty-print summary table iterates the list so users see
  every file written.

### Out of scope
- Cursor SDK adoption (`@cursor/sdk` TypeScript package, public beta
  since 2026-04-29) and Cursor Cloud Agents REST API — both would
  unlock typed `CursorAgentError` / `IntegrationNotConnectedError`
  classification (the same kind of "swallow the error class" hole
  the v0.28-v0.30 chain closed for Claude Code) plus first-class
  hooks / subagents / cloud-VM execution that fits AutoDev's
  tournament topology cleanly. Architectural decision deferred to
  a future v0.31+ — re-introduces in-process complexity that
  v0.26.0's subprocess-only refactor deliberately removed; needs a
  spec doc covering Node.js bridge vs REST-API trade-offs, the
  token-billing cost model, and the migration story for existing
  ``platform: cursor`` workspaces.

## [0.30.0] - 2026-05-13

Polish and observability — final release in the v0.28-0.30 triplet
that closes the infrastructure-failure recovery surface end-to-end.
v0.28.0 stopped the bleed (manual ``autodev requeue`` + classifier
+ probe); v0.29.0 added the typed data model
(``block_reason_class``, ``quarantined``, ``autodev rewind``); this
release adds the structural guarantees that turn the foundation into
compile-time safety. Three bugs land: the phase aggregator now
refuses to auto-accept ANY phase containing an infrastructure-class
block (Bug 3, generalises the v0.29.0 quarantined-only check), the
plan-ledger records ``api_error_status`` and a per-call
``adapter_failure`` audit op so post-mortems no longer require
grepping ``.autodev/debug/*.txt`` (Bug 4), and a cross-task circuit
breaker halts a run after N infrastructure failures in a rolling
window so a single dead token can't burn an entire run before
guardrails fire (Bug 5).

### Added
- Phase aggregator infrastructure-block early bail
  (``src/orchestrator/execute_phase.py:_phase_has_infrastructure_block``
  + ``_pause_phase_for_infrastructure``). Wired in
  ``_maybe_run_phase_review`` immediately after the v0.29.0
  quarantined-check so all four downstream auto-accept sites
  (corrective, A-winner, no-direction, no-corrective) inherit the
  pause for free. Distinct log signal
  (``execute_phase.phase_aggregate_paused_due_to_infrastructure``
  vs the quarantined-path's
  ``phase_review_paused_for_quarantine``) so post-mortems can tell
  the two halt provenances apart even though both stamp the same
  ``review_status="paused"``.
- Ledger payload extension on ``update_task_status`` op: optional
  ``api_error_status`` and ``last_adapter_subtype`` keys merged
  through ``PlanManager.update_task_status``'s existing
  ``payload.update(meta)`` shim. Stamped at the four block sites
  Bug 6 already typed in v0.29.0
  (``execute_phase.py:2138`` worker-exception fallback,
  ``:2713`` developer guardrail,
  ``:2823`` reviewer guardrail,
  ``:2885`` test_engineer guardrail). Forensic-only — no Task
  model field added.
- New ``LedgerOp`` value ``"adapter_failure"``: per-adapter-failure
  audit breadcrumb appended at the ``delegate()`` site
  (``execute_phase.py:3765-3793``) for every ``success=False``
  result regardless of whether the failure is fatal. Payload:
  ``{"task_id", "api_error_status", "subtype", "error", "attempt_n"}``.
  Best-effort — a ledger write failure here MUST NOT mask the
  underlying adapter failure for the caller. Audit-only ``_apply_op``
  handler returns plan unchanged.
- ``InfraFailureCircuitBreaker``
  (``src/orchestrator/circuit_breaker.py``): rolling-window counter
  of infrastructure-class failures (subtypes
  ``{"auth_failed","rate_limited","server_error"}`` — NOT
  ``client_error`` or other deterministic subtypes which are per-task
  verdicts, not infra signals). API: ``record_failure(task_id,
  subtype, ts)``, ``should_halt() -> (bool, reason)``, ``reset()``.
  Time backend: ``datetime.now(timezone.utc)`` matching the
  orchestrator's other UTC-aware time handling. Successful adapter
  results reset the counter (a healthy call clears any prior infra-
  flake history).
- ``InfrastructureCircuitOpenError`` typed exception
  (``src/tournament/errors.py``, sibling of
  ``AuthenticationFailedError``). Raised by the breaker
  integration in ``delegate()`` immediately after the result hook;
  caught at the same five top-level sites as
  ``AuthenticationFailedError`` and treated identically (mark
  in-flight task ``quarantined``, halt phase loop with actionable
  message, exit non-zero). The v0.29.0 halt helpers
  (``_halt_task_for_auth_failed``, ``_halt_for_auth_failed``) now
  accept either exception type via union typing; co-located
  ``_halt_reason_prefix`` helper keeps the ``blocked_reason`` ledger
  prefix distinct (``auth_failed:`` vs ``infra_circuit_open:``) so
  post-mortem grep stays per-typed-halt.
- ``Config.circuit_breaker_threshold: int = 3`` and
  ``Config.circuit_breaker_window_s: float = 60.0``
  (``src/config/schema.py``) with Pydantic ``ge=1`` / ``gt=0.0``
  validators. Tuneable for flaky environments — e.g. raise
  threshold to 6 if you regularly hit small 503 bursts.

### Fixed
- The "phase 1 force-accepted in 0.5s with empty diff" failure mode
  observed during the 2026-05-13 auth wave is now structurally
  prevented at THREE layers: classifier (v0.28.0) routes the failure
  to a typed exception, data model (v0.29.0) gives every block site a
  typed ``block_reason_class``, and aggregator (this release)
  refuses to auto-accept any phase whose composition includes an
  infrastructure-class block. None of the three layers is sufficient
  alone; together they make the pattern unreachable.
- Post-mortem on a thrash now reads from the ledger, not from
  ``.autodev/debug/*.txt`` glob walks. A single grep on the ledger
  for ``op="adapter_failure"`` returns the failure timeline with
  ``api_error_status``, ``subtype``, ``error``, and ``attempt_n``
  per call.
- A single dead corp-proxy token can no longer burn an entire run
  before guardrails (the v0.27 invocation-cap and duration-cap)
  notice. The breaker trips after 3 ``auth_failed``/``rate_limited``
  /``server_error`` results in 60 s by default, raising
  ``InfrastructureCircuitOpenError``, quarantining the in-flight
  task, and halting the phase loop with an operator-facing message.
  Resume picks up zero-touch the moment the underlying problem is
  fixed.

### Migration
- No on-disk schema migrations. Pre-v0.30 plans, ledgers, and
  configs load unchanged. ``circuit_breaker_threshold`` and
  ``circuit_breaker_window_s`` use compile-time defaults when
  absent from the config.
- Forward-compatibility: v0.30 adds ``"adapter_failure"`` to the
  ``LedgerOp`` Literal. A user who runs ``autodev`` under v0.30 and
  then downgrades to v0.29.x cannot ``autodev resume`` —
  pre-v0.30 ``_apply_op`` raises ``LedgerCorruptError`` on the new
  op name. Same downgrade procedure as the v0.28/v0.29 migration
  notes (``autodev reset --hard`` OR remove the offending ledger
  lines manually).

### Out of scope
- Cross-platform adapter parity. The ``cursor.py`` adapter has a
  parallel ``healthcheck`` method but its auth-error response
  shape differs from the Claude CLI's; this triplet addresses the
  ``claude_code`` adapter only. Cursor adapter still surfaces
  failures via the legacy free-text ``error`` string.
- Mutmut kill-rate gate (deferred from v0.27 again — separate work
  stream).

## [0.29.0] - 2026-05-13

Structured recovery surface — typed data model. v0.28.0 stopped the
bleed (manual ``autodev requeue`` + classifier/probe foundation);
v0.29.0 makes the data model match the recovery semantics so the
v0.30.0 structural guarantees in the next release have something
typed to enforce against. Three bugs land: typed
``block_reason_class`` stamped at every block site (Bug 6), the new
``quarantined`` ``TaskStatus`` + ``paused`` ``Phase.review_status``
pair (Bug 7) so ``AuthenticationFailedError`` halts auto-resume
cleanly without the operator needing an explicit ``requeue``, and
``autodev rewind --to-phase`` (Bug 9) for undoing prior force-accepts
that pre-date the v0.28.0 classifier fix.

### Added
- ``Task.block_reason_class: Literal["verdict","infrastructure","cap"] | None``
  field (``src/state/schemas.py``). Stamped at every block site:
  upstream-failure cascade (``plan_manager.py:763``, inherits parent's
  class), architect-consult infrastructure escalation
  (``execute_phase.py:658-666``, ``"infrastructure"``), QA-gate
  timeout / worker exception (``execute_phase.py:1806/1836``, network
  + auth + timeout exceptions classify as ``"infrastructure"``, all
  others ``"verdict"``), and guardrail-exceeded
  (``execute_phase.py:2330-2334``, inspects the orchestrator's
  ``_last_adapter_subtype`` — ``auth_failed``, ``rate_limited``, or
  ``server_error`` → ``"infrastructure"``, else ``"cap"``). Legacy
  plans backfill the class on load by classifying ``blocked_reason``
  against the keyword set in the new ``src/state/infra_patterns.py``
  module (conservative default: ``"verdict"`` when no pattern matches).
- ``src/state/infra_patterns.py`` (new): exports ``INFRA_PATTERNS``,
  ``looks_infrastructure``, and ``classify_blocked_reason`` —
  shared between ``autodev requeue --infrastructure`` (replaces the
  v0.28.0 inline heuristic) and the v0.29.0 plan-load backfill so
  the classification rules live in exactly one place.
- ``Orchestrator._last_adapter_subtype`` and
  ``_last_adapter_api_error_status`` instance fields. Stashed after
  every ``delegate()`` call so the guardrail block sites — fired by
  a different code path than the adapter call — can attribute the
  guardrail to whatever adapter failure preceded it.
- ``TaskStatus.quarantined`` (non-terminal) — ``Orchestrator.resume()``
  picks up quarantined tasks automatically via
  ``_find_in_progress_task`` (extended to include the new status).
  ``AuthenticationFailedError`` catch sites (added in v0.28.0) now
  mark the in-flight task ``quarantined`` instead of ``blocked``;
  ``block_reason_class`` is intentionally NOT stamped on quarantined
  tasks — that field is reserved for true ``blocked`` (a quarantined
  task is awaiting recovery, not classification). Forensic
  ``blocked_reason="auth_failed: <error>"`` is retained.
- ``Phase.review_status="paused"`` — phase aggregator
  (``_maybe_run_phase_review``) refuses to auto-accept a phase
  containing any quarantined task, stamps ``"paused"`` instead, and
  defers the phase-review tournament until ``autodev resume`` clears
  the quarantine. Both auth-halt helpers
  (``_halt_task_for_auth_failed`` and ``_halt_for_auth_failed``)
  ALSO stamp the owning phase ``"paused"`` directly so the post-halt
  plan state is consistent at catch-time — defense-in-depth alongside
  the aggregator's check, idempotent.
- ``autodev rewind`` CLI command — undoes prior force-accepts of
  bogus phases (the v0.28.0 classifier prevents new force-accepts;
  ``rewind`` cleans up the ones that landed before the upgrade).
  ``detect_last_stable_phase()`` (``src/state/rewind.py``) walks
  the ledger and identifies the most recent phase whose
  ``update_phase_meta review_status="accepted"`` was preceded by a
  ``phase_review_complete`` event with matching ``phase_id`` AND
  ``accept_phase=True`` — a force-accept (no preceding tournament)
  is skipped. ``apply_rewind()`` resets affected tasks to ``pending``,
  clears phase ``review_status`` to ``None`` via direct ledger op,
  and MOVES (not deletes) evidence/tournament artifacts to
  ``.autodev/rewound/<YYYYMMDDTHHMMSSZ>-<target_phase_id>/``
  preserving the original sub-tree shape for forensics. Idempotent.
  Flags: ``--to-phase``, ``--dry-run``, ``--yes``.
- New ``LedgerOp`` value ``"rewind"`` — single audit-only entry per
  ``apply_rewind`` call capturing the target phase + before/after
  counts. Replay reproduces the per-task transitions purely through
  the ``update_task_status`` ops emitted alongside.
- Three new task-state transitions registered in
  ``src/orchestrator/task_state.py``: ``in_progress → quarantined``,
  ``blocked → quarantined`` (operator/auth-recovery upgrade path),
  ``quarantined → in_progress`` (back into the normal flow on
  resume).

### Changed
- ``Orchestrator.resume()`` now walks ``paused`` phases first and
  clears the stamp via a direct ``update_phase_meta`` ledger op (the
  canonical helper short-circuits ``None`` as "leave unchanged" —
  same workaround already used by ``PlanManager.requeue_tasks``).
  The in-flight scan then finds the quarantined task via the
  extended ``_find_in_progress_task``. After the task lands, the
  existing post-task ``_maybe_run_phase_review`` poll fires the
  tournament cleanly.
- ``autodev requeue --infrastructure`` keyword list extended with
  the v0.29.0-stamped prefixes (``auth_failed``, ``rate_limited``,
  ``server_error``, ``architect_consult: infrastructure``). The
  legacy public function names ``_INFRA_PATTERNS`` and
  ``_looks_infrastructure`` are kept as compatibility re-exports
  from ``src/cli/commands/requeue.py`` so the v0.28.0 import surface
  is unchanged.
- v0.28.0's ``test_orchestrator_auth_failed_halt`` test contract
  flips from ``blocked`` + ``review_status: None`` to ``quarantined``
  + ``review_status: "paused"``. Documented inline.

### Migration
- No on-disk schema migrations required. Pre-v0.29 plans load
  unchanged; the ``block_reason_class`` field defaults to ``None``
  and is backfilled on load for legacy ``blocked`` tasks via the
  ``classify_blocked_reason`` shim. Pre-v0.29 ledgers replay
  unchanged.
- Forward-compatibility: v0.29 adds ``"rewind"`` to the ``LedgerOp``
  Literal and ``"quarantined"``/``"paused"`` to the
  ``TaskStatus``/``Phase.review_status`` Literals respectively. A
  user who runs ``autodev`` under v0.29 and then downgrades to
  v0.28.x cannot ``autodev resume`` — pre-v0.29 ``_apply_op`` raises
  ``LedgerCorruptError`` on the new op name, and pre-v0.29 Pydantic
  models reject the new status values. Same downgrade procedure as
  the v0.28 migration note (``autodev reset --hard`` OR remove the
  offending ledger lines manually + revert task statuses).

### Out of scope
- v0.30.0 (final release in this triplet) covers the structural
  guarantees that turn the v0.28+v0.29 classifier+data-model
  foundation into compile-time safety: phase aggregator refuses to
  auto-accept ANY phase containing a task with
  ``block_reason_class="infrastructure"`` (extends the v0.29.0
  quarantined-only check), ledger payload records
  ``api_error_status`` for post-mortems without grepping
  ``.autodev/debug/*.txt``, and a cross-task circuit breaker halts
  a run after N infrastructure failures in a rolling window so a
  single dead token can't burn an entire run before guardrails fire.

## [0.28.0] - 2026-05-13

Infrastructure-failure recovery surface — foundation. A real-world run
hit a corp-proxy `ANTHROPIC_AUTH_TOKEN` expiry mid-execute; the adapter
silently swallowed the resulting 403s, the tournament classifier never
recognised them as a typed failure class, and the orchestrator burned
~150 retries thrashing on dead auth before guardrails fired and a phase
was force-accepted with an empty diff. v0.28.0 ships the first of three
planned releases that close this failure mode end-to-end: this release
stops the bleed (a manual escape hatch + the silent-classifier fix +
a startup probe). v0.29.0 will add the typed data model
(`block_reason_class`, `quarantined`, `autodev rewind`); v0.30.0 will
add the structural guarantees (refuse force-accept, ledger
observability, circuit breaker).

### Added
- `autodev requeue` CLI command — flips blocked tasks back to
  `pending` so the operator can resume after transient outside-the-loop
  failures (auth refresh, gateway 4xx, DNS hiccup) without losing the
  surrounding plan structure or the prior tournament work invested in
  each task. Selection flags compose: `--task ID` (repeatable),
  `--phase ID` (repeatable), `--infrastructure` (keyword heuristic
  matching 401/403/Forbidden/authenticate/api_error_status/Connection
  refused/DNS), `--all-blocked`. `--dry-run` previews without writing
  the ledger; `--yes` skips the interactive confirmation.
  `src/cli/commands/requeue.py` (new),
  `PlanManager.requeue_tasks` + `RequeueResult` dataclass.
- `AgentResult.api_error_status: int | None` field. The `claude_code`
  adapter now synthesises a typed `subtype` from `api_error_status`
  (401/403 → `auth_failed`, 429 → `rate_limited`, 5xx → `server_error`,
  other 4xx → `client_error`) in BOTH parse branches (rc!=0 with
  JSON-in-stdout, AND rc=0 success-path with `is_error=true`). The
  CLI's own real subtype (e.g. `error_max_turns`) keeps precedence.
  `src/adapters/claude_code.py:_api_status_to_subtype`.
- `AuthenticationFailedError` typed exception
  (`src/tournament/errors.py`, subclass of `TournamentError`). Raised
  by the tournament retry wrapper on `auth_failed` subtype.
  `run_execute_phase` and the underlying DAG dispatchers
  (`_execute_phase_dag`, `_execute_cross_phase_dag`,
  `_execute_one_worker`) catch it, mark the in-flight task `blocked`
  with `blocked_reason="auth_failed: <error>"`, log
  `execute_phase.auth_failed_halt`, surface an operator-facing console
  message, and re-raise so the CLI exits non-zero. Phase review is
  intentionally never triggered on the halt path — force-accepting a
  half-empty phase on dead credentials is the production stall this
  fix exists to prevent.
- `claude_code` adapter `healthcheck()` second-stage PONG probe
  (`echo PONG | claude -p --max-turns 1`, 10s timeout). Stage 1
  (`claude --version`) still catches a missing CLI; stage 2 catches
  the case a working binary masks expired auth or a broken upstream.
  Returns `(False, "auth_failed: ...")` on 401/403,
  `(False, "network: ...")` on timeout. The abstract
  `PlatformAdapter.healthcheck` contract documents these reason
  prefixes so callers can route on them.
- Mandatory preflight re-probe in `autodev resume` and
  `autodev execute` immediately before entering the orchestrator loop.
  Re-runs `await adapter.healthcheck()` (NOT cached) — users typically
  invoke `resume` right after fixing auth, so a stale negative would
  lock them out. On probe failure both commands exit 2 and print an
  actionable refresh-auth block (verify `ANTHROPIC_API_KEY`, refresh
  `ANTHROPIC_AUTH_TOKEN`, or run `claude /login`). The Orchestrator
  is never constructed if the probe fails, so no LLM spend is incurred
  for a known-broken environment.
- New `LedgerOp` value `"requeue"` — audit-only breadcrumb appended
  alongside the per-task `update_task_status` ops emitted by
  `requeue_tasks`. Replay reproduces the per-task transitions purely
  through the `update_task_status` ops.

### Fixed
- 401/403/429/5xx responses from the Claude CLI are no longer silently
  swallowed into a free-text `error` string and downstream
  `"empty reviewer response"` placeholder. The tournament classifier
  now sees a typed signal and short-circuits: `auth_failed` and
  `client_error` are treated as deterministic (no retry); `rate_limited`
  and `server_error` are classified as transient (retry via tenacity
  backoff).
- `autodev resume` now reports infrastructure problems (bad auth,
  network) at startup with an actionable message instead of building
  the Orchestrator and thrashing retries against a dead endpoint.

### Migration
- No on-disk schema migrations. Existing `.autodev/config.json`,
  `plan.json`, and pre-v0.28 ledgers load unchanged.
- Forward-compatibility: v0.28 adds `"requeue"` to the `LedgerOp`
  Literal. A user who runs `autodev` under v0.28 and then downgrades
  to v0.27.x cannot `autodev resume` — pre-v0.28 `_apply_op` raises
  `LedgerCorruptError` on the new op name. Same downgrade procedure
  as the v0.27 migration note (`autodev reset --hard` OR remove the
  offending ledger lines manually).

### Out of scope
- v0.29.0 covers the typed data-model overhaul: `Task.block_reason_class`
  (replaces the v0.28 `--infrastructure` keyword heuristic with a typed
  field stamped at every block site), the `quarantined` `TaskStatus`
  (so `AuthenticationFailedError` halts auto-resume cleanly without
  needing an explicit `requeue`), `Phase.review_status="paused"`, and
  `autodev rewind --to-phase` for undoing prior force-accepts.
- v0.30.0 covers the structural guarantees that turn this release's
  classifier+probe foundation into compile-time safety: phase aggregator
  refuses to auto-accept a phase containing infrastructure-class blocks,
  ledger observability records `api_error_status`, and a cross-task
  circuit breaker halts a run after N infrastructure failures in a
  rolling window.

## [0.27.0] - 2026-05-12

Autonomy-stability audit. v0.26.2's persistent-failure drop closed one
class of architect-output failure; the v0.27 chain hardens every adjacent
layer so plans run end-to-end on the standard architect-output failure
modes with zero operator intervention. Eleven phases shipped across nine
commits (mutmut gate deferred to v0.28 — see Out of scope).

### Fixed
- Diff-scoped QA gates (secretscan, hallucination_guard, mutation_test,
  code_size) no longer silently pass when the developer ships a
  non-empty but unparseable diff body. `extract_files_from_diff(strict=True)`
  raises `DiffParseError`; `_run_qa_gates` translates that into a
  blocking failure for diff-producing tasks. Investigation tasks opt
  out via the new `Task.produces_diff=False` field. (Phase 6 / audit §6)
- secretscan emits an explicit info-severity skip when `paths=[]`
  instead of silently scanning nothing. (Audit §6.3)
- edit_scope violations now block only the offending task instead of
  every pending task across every phase. The v0.26.2 blanket-block
  fallback still fires when every pending task violates the scope
  (preserves safety for truly-broken plans). (Phase 3 / audit §3)

### Improved
- Plan parser strips hedge text from architect-emitted paths at parse
  time (paren-hedge, inline comments, placeholder tokens like `TBD`,
  multi-word phrases without slashes). Closes a family of "architect
  emitted hedge, persistent-drop cleaned it up after three retries"
  failures upstream — most plans now validate on the first architect
  attempt. Legitimate paths with spaces (e.g. `docs/My File.md`) are
  preserved via the explicit "has slash" check. (Phase 1 / audit §1)
- Persistent-failure drop now also walks `task.files_new` as a fallback,
  enforces an empty-guard at the phase-edit_scope level (refuses to
  silently widen back to plan scope), and auto-skips tasks whose
  files + files_new are both empty after a drop. Phase-level granular
  ledger ops let forensics pin which `(task_id, phase_id)` lost an
  entry. (Phase 4 / audit §4)
- Architect retry-envelope payload is now a Pydantic
  `TypedRetryEnvelope` model rather than an inline dict, catching
  field-name typos at construction time. Wire format unchanged.
  (Phase 4 prep / audit §4)
- Hedge-repro integration test pinned to the v0.27 fixed-behaviour
  spec: paren-hedged `Task.files` recovers via Phase 1 parser in one
  architect attempt; bare-token EDIT_SCOPE entries recover via the
  v0.26.2 persistent-drop in three. (Phase 0 → Phase 11)

### Added
- Phase 0 test infrastructure: hedge-text fixture library at
  `tests/fixtures/`, exhaustive LedgerOp handler check (would have
  caught v0.26.1's missing `architect_consult` handler at PR time),
  deterministic hedge-pattern reproducer. No `src/` changes — the
  regression spec every later v0.27 phase tightens against.
  (Phase 0)
- `Task.produces_diff: bool = True` field. Investigation tasks set
  `False` to opt out of the fail-closed diff-scoped gate.
- `errors.DiffParseError`, `errors.EmptyDiffScopeError` typed exceptions.
- `orchestrator.plan_parser.ParsedFilesReport` dataclass +
  `_normalize_path_entry` helper. Drop reasons are structured strings
  (`paren_hedge`, `bracket_hedge`, `placeholder`, `space_without_slash`,
  etc.) logged with `task_id` for forensics.
- `orchestrator.retry_envelope.TypedRetryEnvelope` + `PriorError`
  Pydantic models. `extra="forbid"` so typos surface at construction
  time.
- Post-tournament structural-validity gate. The refined plan markdown
  must parse cleanly AND every listed path must exist on disk (or
  be `[new]`-tagged) — failures fall back to the pre-tournament
  plan and emit `tournament_output_rejected_structurally` for
  forensics. (Phase 5 / audit §5)
- `dag.collect_edit_scope_violations`: returns a list of violations
  with task / phase / file_path metadata attached, replacing the
  raise-first-then-discard contract used internally before v0.27.
- `orchestrator.escalation_envelope.parse_escalation_line`: detects
  the `ESCALATE: <reason>` line every role prompt is now instructed
  to emit when genuinely blocked. Pairs the prompt-level autonomy
  clause (see Changed) with the runtime detector so blocked agents
  route to architect-consult instead of burning a retry cycle.
  (Phase 7 / audit §7)
- `Makefile` with `test`, `test-stability`, `mutate-parser` targets.
- 10 new audit-only ledger ops: `task_files_entry_dropped`,
  `task_files_new_entry_dropped`, `task_extended_scope_entry_dropped`,
  `phase_edit_scope_entry_dropped`, `task_auto_skipped`,
  `architect_persistent_parse_error`, `architect_persistent_pyd_error`,
  `tournament_output_rejected_structurally`,
  `task_blocked_scope_violation`, `agent_escalated`. All emitted
  alongside existing catch-all ops so v0.26.2 forensics tooling keeps
  working.

### Changed
- Architect prompts now embed a structured-field-discipline section
  spelling out the parser's hedge-text drop rules and the three
  supported ways to express uncertainty (omit, `[new]`, or
  Extended-scope+Justification). Pairs with the parser hardening in
  Phase 1. (Phase 2 / audit §2)
- All 14 role prompts embed a shared autonomy clause: roles do not
  ask clarifying questions; when genuinely blocked they emit a
  single `ESCALATE: <reason>` line that the orchestrator routes to
  architect-consult. The legacy "would you like to create a spec?"
  prompt in `architect.md` is reworked into a one-shot decision.
  (Phase 7 / audit §7)
- `extract_files_from_diff` accepts a new `strict=False` keyword
  argument. Default preserves v0.26.2 behaviour for legacy callers
  (phase_review_runner). The QA-gate site opts in via `strict=True`
  to fail-closed on garbage diff bodies.

### Migration
- **Ledger forward-compatibility**: v0.27 introduces 10 new audit-only
  ledger ops. A user who runs `autodev` under v0.27 and then downgrades
  to v0.26.2 cannot `autodev resume` — v0.26.2's `_apply_op` raises
  `LedgerCorruptError` on the new op names. To downgrade, first run
  `autodev reset --hard` (destroys plan state) OR manually edit
  `.autodev/plan-ledger.jsonl` to remove lines whose `op` field matches
  any of the 10 new op names listed above. Forward compatibility within
  the v0.27.x series is preserved.
- No on-disk schema migrations. Existing `.autodev/config.json`,
  `plan.json`, and pre-v0.27 ledgers load unchanged.

### Out of scope
- `mutmut` kill-rate gate (Commit 4) deferred to v0.28: mutmut 3.x's
  CLI differs from the original plan's expected interface (config-file
  driven rather than `--paths-to-mutate` flag). Phase 0's unit
  coverage on `plan_parser` gives enough confidence to ship without
  mutation testing in v0.27.
- P1 phases 8 (pipeline-wide typed retry envelope), 9 (doctor --deep
  + ledger integrity validator), and 10 (cost cap + BOM + drain
  timeout + plateau-on-default) are not in this release. Tracked for
  v0.28.

## [0.26.2] - 2026-05-12

Architect-retry diagnostic + persistent-failure drop. v0.26.1's first
real-world Unity QNX run failed when the architect emitted `notes` as a
literal `EDIT_SCOPE:` entry, the validator correctly rejected it on
attempt #1 + retry #1, and the run died with **zero recoverable
diagnostics** — the architect's failed markdown was never persisted, and
the retry envelope passed `str(exc)` instead of the structured `(raw,
reason, suggestion)` fields the architect needed to fix the path.
v0.26.2 ships four phases that close this failure class without
loosening any existing safety property.

### Fixed
- Architect plan-validation failures now persist the rejected markdown
  to `.autodev/debug/architect-failed-<unix-ms>.md` so the operator can
  diagnose the bad output without re-running the entire plan phase.
  Triggered on `PlanParseError`, `PydValidationError`, AND
  `PathValidationError`. New helper:
  `orchestrator.plan_phase._persist_failed_architect_plan`. (Phase 1a)

### Improved
- When the architect emits a path that fails `validate_files_exist`,
  the retry envelope now passes typed `path_error_raw`,
  `path_error_reason`, and `path_error_suggestion` fields as separate
  context keys. The architect can correct just the bad path instead of
  re-drafting the whole plan from the stringified exception. (Phase 1b)
- Architect prompt (`src/agents/prompts/architect.md`) carries a
  positive-only validation note clarifying that `EDIT_SCOPE:` entries
  are tested against `git ls-files` and pointing the architect at the
  new `path_error_*` retry fields. NO forbid-list — per the `/critic`
  finding, negation phrasing risks both schema-contradiction and
  LLM-negation-inflation. (Phase 4)

### Added
- Bounded architect-retry loop with persistent-failure drop. After 3
  architect attempts where the same `(raw, reason)` recurs, the
  orchestrator drops the bad scope entry from `plan.edit_scope`,
  `phase.edit_scope`, `task.files`, and `task.extended_scope`, appends
  a typed `scope_entry_dropped` ledger op, and continues. New
  module-level constants `_MAX_ARCHITECT_ATTEMPTS = 3` and
  `_DROP_AT_RECURRENCE = 3` (distinct names for the same numeric value
  — they mean different things). New helpers in
  `orchestrator.plan_phase`: `_validate_with_persistent_drop`,
  `_drop_entry_from_plan`, `_build_retry_env`. (Phase 3)
- **Hard empty-scope guard**: drops that would leave
  `plan.edit_scope == []` (the documented whole-repo sentinel) are
  refused — the original `PathValidationError` is re-raised. Silent
  widening to whole-repo is a P0 risk this guard prevents. Two of the
  six Phase-3 tests assert this guard fires. (Phase 3)
- `LedgerOp` Literal extends with `"scope_entry_dropped"`. Audit-only —
  no plan mutation on replay (the new plan with the dropped entry is
  persisted via the `init_plan` op alongside). Payload shape:
  `{path, reason, suggestion, attempt, recurrence_count}`. Also adds
  the missing no-op handler for `"architect_consult"` (v0.26.1 op that
  was registered in the Literal but had no `_apply_op` handler — would
  crash on `replay_ledger`). (Phase 3)

### Migration
- Zero on-disk migration. Existing configs and plans load identically.
  The new ledger op is append-only; no schema migration needed.
- Operator-facing: when a run hits the new drop path, look in
  `.autodev/debug/` for `architect-failed-*.md` files AND grep the
  plan-ledger for `scope_entry_dropped` ops to see what got dropped.

## [0.26.1] - 2026-05-12

QA-gate encoding fix + diagnostic surfacing + architect-consult escalation
rung. The first full Unity-scale C++ run on v0.26.0 (2026-05-11) exposed
six concrete defects in the QA-gate + escalation layer plus one missing
feature; v0.26.1 ships all seven as one release. Orchestrator core
behavior (subprocess dispatch, tournaments, FSM) is unchanged.

### Fixed
- UTF-8 decode crash on user source files with non-ASCII bytes (Latin-1
  surnames in vendored copyright headers, mixed-encoding ASCII art, etc.).
  Introduces ``qa._io.safe_read_source`` which centralizes the
  ``read_text(errors="replace")`` contract; three scanners
  (``qa.cpp_symbols.scan_cpp_file``,
  ``qa.hallucination_guard._scan_python_file``,
  ``qa.hallucination_guard._scan_typescript_file``) previously crashed
  on the first non-UTF-8 byte instead of substituting ``U+FFFD``.
  (patches A, B, C)
- ``qa.hallucination_guard._SKIP_DIRS`` extended to skip vendored trees
  (``External``, ``Tools``, ``vendor``, ``third_party``, ``third-party``)
  in addition to the legacy ``.git`` / ``.venv`` / ``node_modules`` /
  build-artifact set. Operators with project-specific vendor directories
  can extend the default set via ``cfg.qa_gates.hallucination_guard_skip_dirs``
  (the operator list is UNIONED with the default, never replacing it).
  (patch B)
- ``_files_changed_for_secretscan`` contract flipped from "``None`` when
  no diff → callers do a legacy full-walk" to "``[]`` when no diff →
  callers scan nothing". The full-walk fallback was a footgun on huge
  vendored trees. Affected downstream gates: ``secretscan``,
  ``hallucination_guard``, ``mutation_test``, ``code_size``. (patch C)

### Improved
- Adapter failure modes now surface with the typed ``subtype`` (e.g.
  ``error_max_turns``, ``error_max_tokens``) and the first 200 chars of
  the adapter's error in the escalation reason. Previously every
  coder-adapter failure produced the identical literal
  ``"coder adapter failure"`` regardless of underlying cause, so the
  repetition_loop course-correction misfired by matching on the
  cosmetic symptom. New helper:
  ``orchestrator.execute_phase._build_adapter_failure_reason``.
  (patch D)
- ``_execute_one_worker`` now classifies caught exceptions by
  ``isinstance`` and emits a typed ``blocked_reason`` prefix
  (``qa_gate_encoding_error`` / ``qa_gate_io_error`` /
  ``qa_gate_timeout`` / ``worker_exception``). The full traceback is
  persisted to ``.autodev/debug/worker-exception-<task>-<ts>.txt`` via
  ``state/paths.py:debug_dir`` for operator action without re-running
  with verbose logging. (patch E)

### Changed
- ``GuardrailsConfig.max_duration_s_per_task`` default bumped from
  ``900`` to ``2400`` seconds. The 900s default predated the v0.8.0
  per-complexity timeout escalation (``TASK_TIMEOUT_S_DEFAULTS["complex"]
  = 1800``); legitimate complex tasks could consume their entire
  subprocess budget and trip the wall-clock guardrail before the
  reviewer could run. Operators with explicit values are unaffected —
  only the default changed. (patch F)
- FSM extension: ``in_progress -> skipped`` transition is now allowed
  (was ``pending -> skipped`` only) so the architect-consult
  ``refine-tasks`` resolution can supersede a failing task with
  corrective sub-tasks. (patch G)

### Added
- ``ARCHITECT_CONSULT`` escalation rung
  (``orchestrator.escalation_ladder``). When the developer cannot
  figure out a failing task and the autonomous escalation budget is
  exhausted (``search_count >= 3``), the orchestrator re-delegates to
  ``architect_b`` in CONSULT MODE for a final structured intervention
  before terminal handoff. One-shot per task — after the architect has
  weighed in (``architect_count >= 1``), the next escalation falls
  through to SOFT_BLOCKER. New prompt:
  ``src/agents/prompts/architect_b_consult.md``. New ledger op:
  ``architect_consult``. The architect returns one of three
  resolutions:
  - ``RESOLUTION: refine-tasks`` — bullet list of corrective sub-tasks.
    Orchestrator appends them via the existing
    ``plan_manager.append_corrective_tasks`` pipeline and marks the
    failing task as ``skipped`` with metadata
    ``architect_consult_action="refine"``.
  - ``RESOLUTION: infrastructure`` — environment / tooling diagnosis.
    Orchestrator marks the task ``escalated`` + ``blocked`` with
    ``escalated_infra=True`` in metadata; surfaces the architect's
    diagnosis in ``blocked_reason``.
  - ``RESOLUTION: continue`` — the developer was on the right track.
    Orchestrator resets ``retry_count`` to 0 and puts the task back to
    ``in_progress``.
  Mimics human-team behavior: junior dev struggles → asks the senior
  who designed the plan → applies their guidance → escalates to human
  only if still stuck. (patch G)
- ``StuckState.architect_count`` field +
  ``PlanManager.increment_architect_consult`` mirror the existing
  ``pivot_count`` / ``search_count`` per-task accounting. (patch G)

### Migration
- Zero on-disk migration needed. Existing ``.autodev/config.json`` and
  ``.autodev/plan.json`` files load and run identically. Operators
  with ``cfg.guardrails.max_duration_s_per_task`` set explicitly retain
  their value (only the default changed).
- ``_files_changed_for_secretscan`` return-type tightened from
  ``list[Path] | None`` to ``list[Path]``. The function is module-
  private (underscore-prefixed); no public consumers exist.
- ``run_hallucination_guard`` accepts a new ``extra_skip_dirs`` keyword
  argument (default ``None``). Existing callers continue to work.

### Tests
~25 new tests (4 qa_io + 3 _SKIP_DIRS + 5 secretscan/hallucination_guard
contract flip + 4 retry-reason + 5 worker-exception + 1 config + 11
architect_consult + 5 ladder regressions). Total suite: ~2,411 passed.

## [0.26.0] - 2026-05-12

Subprocess-only architecture. ``InlineAdapter`` and the file-based
delegation/response state machine that backed inline mode in <=v0.25.x
are deleted; every dispatch is now a subprocess via ``ClaudeCodeAdapter``
or ``CursorAdapter``. The ``/autodev`` slash command shells out to the
``autodev`` CLI for every invocation (the v0.24.2 transition already
made the inline distinction architectural dead-weight; v0.26.0 takes
the deletion).

This is a **public API removal**: ``platform: "inline"`` in
``.autodev/config.json``, the ``--inline`` flag on ``autodev init``,
the ``DelegationPendingSignal`` ↔ ``autodev resume`` flow,
``.autodev/inline-state.json``, ``.autodev/delegations/``, and
``.autodev/responses/`` are all gone. Legacy on-disk configs auto-migrate
(see Migration notes below) and the ``--inline`` flag survives as a
deprecated noop alias until v0.27.0.

### Removed
- ``src/adapters/inline.py`` (``InlineAdapter`` class — 270 LOC).
- ``src/adapters/inline_types.py`` (``DelegationPendingSignal``,
  ``InlineSuspendState``, ``InlineResponseFile``, ``InlineResponseError``
  — 92 LOC).
- ``src/orchestrator/inline_state.py`` (``write_suspend_state``,
  ``load_suspend_state``, ``clear_suspend_state`` — 64 LOC).
- ``src/orchestrator/preflight.py`` (introduced in v0.25.4 to guard the
  InlineAdapter ↔ tournaments mismatch — now unrepresentable, so the
  whole module is gone).
- ``src/errors.py``: ``TournamentAdapterMismatchError`` class (same
  reason — the mismatch can no longer happen).
- Inline-mode branches in ``src/orchestrator/__init__.py`` (``resume``),
  ``src/orchestrator/plan_phase.py`` (``_delegate`` suspend/resume),
  ``src/orchestrator/execute_phase.py`` (``_delegate`` suspend/resume,
  ``DelegationPendingSignal`` worker re-raises, ``run_execute_phase``
  preflight call), and the defense-in-depth typed raise in each
  tournament runner.
- ``render_claude_resume_config`` and ``render_cursor_resume_config``
  from ``src/adapters/inline_config.py`` (the auto-resume CLAUDE.md /
  ``src.mdc`` renderers that told the host agent to read delegations
  and run ``autodev resume``).
- Inline-mode test files: ``tests/test_adapter_inline.py``,
  ``tests/test_inline_state.py``, ``tests/test_inline_types.py``,
  ``tests/test_orchestrator_inline_suspend.py`` (≈1,000 LOC). Plus
  trims to ``test_inline_config.py``, ``test_paths_inline.py``,
  ``test_cli_inline.py``, ``test_tournament_adapter_mismatch.py``.

### Changed
- ``autodev init`` defaults to ``platform: claude_code`` (was
  ``"auto"``). The ``--inline`` flag is a deprecated noop alias: prints
  a one-line deprecation warning and sets ``cfg.platform = "claude_code"``.
  Removed in v0.27.0.
- ``/autodev`` slash-command template (``.claude/commands/autodev.md``)
  no longer references inline mode; both the ``--review`` and one-shot
  flows now bootstrap via ``autodev init --force`` and dispatch via
  ``autodev plan --platform claude_code`` / ``autodev execute --platform
  claude_code``. The ``--platform`` flag is explicit at the slash-command
  surface so even legacy on-disk configs with ``platform: inline`` cannot
  leak through.
- ``autodev resume`` no longer special-cases ``inline-state.json``. With
  InlineAdapter gone, resume just continues from the ledger's first
  non-terminal task; the inline suspend/resume entry path was
  architectural dead-weight after v0.24.2.
- ``autodev reset --hard`` continues to purge ``.autodev/delegations/``,
  ``.autodev/responses/``, and ``inline-state.json`` as **legacy
  migration cleanup** (marked as such in the help text and code
  comments). Scheduled for removal from the ``--hard`` set in v0.27.0.

### Migration
- **``platform: "inline"`` in ``.autodev/config.json``** auto-migrates
  to ``"claude_code"`` on config load. A Pydantic model validator
  (``AutodevConfig._migrate_inline_platform``) emits a
  ``DeprecationWarning`` and rewrites the field. The Literal still
  accepts ``"inline"`` for one release; v0.27.0 will narrow it to
  ``{"claude_code", "cursor", "auto"}`` and the migration helper will
  be removed.
- **``autodev init --inline``** still exits 0; it now prints a one-line
  deprecation warning and writes ``platform: claude_code`` to config.
- **Pre-v0.26.0 workspace residue** (``.autodev/delegations/``,
  ``.autodev/responses/``, ``.autodev/inline-state.json``) is no longer
  written but is still cleaned up by ``autodev reset --hard``.
- **The ``/autodev`` slash command template** in pre-v0.26.0
  ``.claude/commands/autodev.md`` references ``autodev init --inline``
  on bootstrap. Re-run ``autodev init --force`` once to regenerate the
  template with the subprocess-only routing.

### Tests
- Net change: **2,386 passed / 7 expected skips** (was 2,448 in
  v0.25.4). ~50 inline-mode tests removed; a handful of subprocess-only
  regression tests retained (``test_init_inline_flag_is_deprecated_noop``,
  ``test_subprocess_adapter_with_all_tournaments_enabled_constructs_cleanly``).

### Internal
- ``src/adapters/__init__.py``: dropped the ``InlineAdapter``
  re-export. The factory now returns only ``ClaudeCodeAdapter`` or
  ``CursorAdapter``; the unknown-platform branch raises ``AdapterError``.
- ``src/adapters/detect.py``: ``PlatformName`` Literal narrowed from
  ``{"claude_code", "cursor", "inline"}`` to ``{"claude_code", "cursor"}``;
  the env-var validator drops ``"inline"`` from the accept-set.
- ``src/cli/commands/plan.py``, ``execute.py``, ``resume.py``: every
  ``cast(... 'inline' ...)`` Literal was narrowed to drop the inline
  arm. ``execute.py`` no longer catches ``DelegationPendingSignal``
  (the only place that did).
- ``docs/design_documentation/adapters_design.md``: revised to describe
  the subprocess-only architecture; all InlineAdapter sections marked
  as historical with explicit v0.26.0 supersession notes.
- ``README.md``: updated the "Adapters" table and the "Quickstart"
  snippet to drop the ``--inline`` flag and reframe the host-agent
  story as "AutoDev shells out via subprocess for every dispatch".

## [0.25.4] - 2026-05-11

Fail-fast guard for the InlineAdapter + tournaments mismatch surfaced
by v0.25.3.

After v0.25.3 made every tournament run by default (the README's #1
discipline mechanism), the first ``/autodev <feature>`` from inside
Claude Code on a workspace with ``platform: inline`` crashed with a
bare ``AssertionError: "Tournament runners must use subprocess
adapters, not InlineAdapter"``. The assert was deep inside the
tournament runner — after spec.md was written and (sometimes) after
the architect's first plan draft — and offered no recovery guidance.
v0.26.0 will delete ``InlineAdapter`` entirely; v0.25.4 ships the
clean failure mode users need *today*.

### Fixed
- **Typed exception with operator guidance.** Bare ``AssertionError``
  on ``InlineAdapter`` + enabled tournament now raises
  :class:`TournamentAdapterMismatchError` (a subclass of
  :class:`ConfigError`). The message names every enabled tournament,
  explains why the combination is architecturally incompatible
  (InlineAdapter's ``parallel()`` raises ``NotImplementedError``;
  tournaments fan out IAG-isolated branches via ``parallel()``), and
  lists the two actionable fixes: set ``platform: claude_code`` (or
  ``cursor``) in ``.autodev/config.json``, or disable tournaments
  via ``tournaments.<phase>.enabled: false``.
- **Preflight check at phase entry.** ``run_plan_phase`` and
  ``run_execute_phase`` now call
  :func:`orchestrator.preflight.check_tournament_adapter_compatibility`
  before any file write or LLM call — so the operator hits the typed
  error *immediately*, not after the architect has drafted a plan.
- **Runner-level guards promoted to explicit raises.** The three
  bare ``assert`` statements in ``plan_tournament_runner``,
  ``impl_tournament_runner``, and ``phase_review_runner`` are now
  ``if isinstance(...): raise TournamentAdapterMismatchError(...)``
  — defense-in-depth that survives ``python -O``.

### Added
- ``src/errors.py``: :class:`TournamentAdapterMismatchError` extends
  :class:`ConfigError`. Carries ``enabled_phases`` for callers that
  want to render their own message.
- ``src/orchestrator/preflight.py``: centralized adapter ↔ tournament
  compatibility check. Called at the entry of both phase loops.

### Tests
- 11 new tests in ``tests/test_tournament_adapter_mismatch.py``
  covering the new exception's shape, the preflight check on each
  tournament type (plan / impl / phase_review), the negative path
  (subprocess adapter, all tournaments disabled), and the runner-level
  defense-in-depth raise.
- ``tests/test_orchestrator_inline_suspend.py`` fixture updated to
  also disable ``phase_review`` (added in v0.21.0) — the legacy
  fixture only disabled ``plan`` and ``impl`` because the v0.25.3
  preflight didn't exist when the test was written.
- Full suite: **2,448 passed / 7 expected skips** (was 2,437 in
  v0.25.3).

### Migration / operator notes
- **Workspaces with ``platform: inline`` and tournaments enabled**
  will now fail at the *start* of ``autodev plan`` (or ``autodev
  execute``) with the typed error and the actionable fix path. No
  partial work, no crash deep in the call stack.
- **v0.26.0** will delete ``InlineAdapter`` entirely and default
  ``autodev init`` to ``platform: claude_code``.

## [0.25.3] - 2026-05-11

Tournaments must never be skipped by default. AutoDev's goal is to
improve the quality and consistency of AI-generated code regardless of
model cost; the prior built-in default of ``auto_disable_for_models:
["opus"]`` silently turned off every tournament for every Claude Code
install (because Claude Code's default model is Opus), defeating the
README's #1 discipline mechanism and reducing the orchestrator to a
single-pass dispatch.

### Fixed
- **All three tournament types (plan, impl, phase_review) run by
  default on every model, including Opus.** The auto-disable mechanism
  is retained as an explicit operator override for cost-controlled
  development environments, but its built-in default is now ``[]`` for
  every tournament type.
- **Per-tournament ``auto_disable_for_models``.** The list moves from
  the top-level :class:`TournamentsConfig` to each
  :class:`TournamentPhaseConfig`, so operators can override one
  tournament without touching the others. A v0.25.3 model-validator
  resolves each per-tournament slot at validation time:
  1. an explicit per-tournament value wins;
  2. otherwise, if the deprecated top-level
     ``tournaments.auto_disable_for_models`` is non-empty, it is
     inherited down (back-compat path for legacy on-disk configs);
  3. otherwise, the per-tournament default is ``[]``.
- **Runner-side wiring**. ``plan_tournament_runner``,
  ``impl_tournament_runner``, and ``phase_review_runner`` now consult
  their own per-tournament list (``cfg.tournaments.<phase>.auto_disable_for_models``)
  rather than the deprecated top-level. Regression tests assert the bare
  top-level read is gone.

### Changed
- ``TournamentsConfig.auto_disable_for_models`` default flipped from
  ``["opus"]`` to ``[]``. The field is kept for back-compat with v0.25.2
  on-disk configs (an explicit non-empty value still inherits to
  every per-tournament slot whose own value is ``None``).
- ``default_config()`` in ``src/config/defaults.py`` updated to match.

### Migration / operator notes
- **Existing workspaces**: ``.autodev/config.json`` files written by
  v0.25.2 and earlier pin ``tournaments.auto_disable_for_models:
  ["opus"]`` to disk. Until refreshed, those workspaces keep the legacy
  behavior (all tournaments skipped on Opus). To pick up the new
  defaults, run ``autodev init --inline --force`` in the workspace, or
  hand-edit ``config.json`` and remove the legacy line.
- **Per-tournament override**: to skip a specific tournament for a cost
  budget (e.g. dev environments), set ``tournaments.<phase>.auto_disable_for_models:
  ["opus"]`` in ``.autodev/config.json``. The other two tournaments are
  unaffected.

### Tests
- 10 new tests in ``tests/test_tournaments_auto_disable_per_phase.py``
  covering the new defaults, back-compat inheritance, explicit override,
  and runner-side wiring.
- Two legacy fixtures
  (``test_plan_phase_tournament_auto_disabled.py``,
  ``test_impl_tournament_auto_disabled.py``) updated to set the per-
  tournament field directly so the operator-override path is exercised
  by the existing assertion suite.
- Full suite: 2,437 passed / 7 expected skips (was 2,427 in v0.25.2).

## [0.25.2] - 2026-05-11

Operator-toolkit release. Implements the four CLI subcommands that
``autodev --help`` and the README had advertised since the early
project phases but were never landed (``reset``, ``logs``, ``prune``,
``execute --dry-run``). The blocker that prompted this was the unity
recovery flow at v0.25.1: ``/autodev reset --hard`` from inside Claude
Code returned ``exit 1`` with no path forward. Now it works.

### Added — `autodev reset [--hard]`
- **Default** clears ``plan.json`` and ``plan-ledger.jsonl``. Frees ``autodev plan`` to write a fresh plan.
- **`--hard`** additionally clears ``evidence/``, ``delegations/``, ``responses/``, ``inline-state.json``, ``tournaments/``, ``sessions/``, ``debug/``, the orphan ``.lock``, and the ``execute_worktrees`` / ``execute_worktrees_pool`` directories.
- **Always preserved** (both modes): ``config.json``, ``spec.md``, ``secretscan-baseline.json``, ``.gitignore``, ``knowledge.jsonl``, ``rejected_lessons.jsonl``, and the v0.25.0 file index (``index.db`` + sidecars + ``index.state.json``). The file index is durable and expensive to rebuild; the knowledge ledger holds cross-run learning that should survive a reset.
- Prints a Rich table of the removed paths so the operator can audit.
- Idempotent on empty / partially-empty workspaces ("nothing to reset" message, exit 0).
- **6 new tests** in ``tests/test_cli_reset.py``. (`src/cli/commands/reset.py`)

### Added — `autodev logs [--session SID] [--follow]`
- New per-session file sink at ``.autodev/sessions/{session_id}/events.jsonl``. The Orchestrator opens the file once after minting its session id; every structlog emission whose bound ``session_id`` matches is appended.
- Without ``--session``, the command tails the session whose ``events.jsonl`` has the most recent mtime.
- ``--follow`` (alias ``-f``) prints existing content then polls for new lines every 250 ms until Ctrl-C (tail -f style).
- Exit 1 with a clear message when no sessions exist or the requested session has no events file — no stack traces.
- **9 new tests** across ``tests/test_cli_logs.py`` (5) and ``tests/test_autologging.py`` (4 file-sink tests added to the existing 6).
- (`src/autologging.py`, `src/cli/commands/logs.py`, `src/orchestrator/__init__.py`)

### Added — `autodev prune [--older-than 30d] [--dry-run]`
- Walks ``tournaments/``, ``sessions/``, and ``evidence/`` and removes children older than the threshold (per-child mtime).
- ``--older-than`` accepts ``Ns`` / ``Nm`` / ``Nh`` / ``Nd``; default ``30d``. Invalid duration → exit 1 with the offending string echoed.
- ``--dry-run`` lists what would be removed without deleting (separate from ``execute --dry-run``).
- Always preserves ``plan.json``, ``plan-ledger.jsonl``, ``knowledge.jsonl``, ``rejected_lessons.jsonl``, ``config.json``, ``spec.md``, and the file index. Use ``autodev reset`` for those.
- **16 new tests** in ``tests/test_cli_prune.py`` (4 functional + 12 parametrized parser cases). (`src/cli/commands/prune.py`)

### Added — `autodev execute --dry-run`
- Loads the plan, prints a Rich task table (Phase | Task | Title | Depends | Status), then a per-phase dispatch listing that respects ``depends_on``: tasks group into successive parallelism windows up to ``cfg.tournaments.execute_max_parallel_tasks`` (default 4).
- Never invokes any agent adapter, never instantiates the Orchestrator — preview-only, zero LLM spend.
- Exit 1 with a hint when no plan is on disk.
- **4 new tests** in ``tests/test_cli_execute_dry_run.py``. (`src/cli/commands/execute.py`)

### Changed — phase-tagged TODO markers retagged to version-based deferrals
- `src/autologging.py:54-58` — **removed** (file sink shipped; the TODO is resolved).
- `src/orchestrator/__init__.py:1-13` — module docstring rewritten to drop the "Phase 4" framing; the impl-tournament integration TODO retagged to ``TODO(v0.26+)``.
- `src/orchestrator/execute_phase.py:9, 13` — ``TODO(phase-8)`` (auto-gates) and ``TODO(phase-7)`` (ImplementationTournament integration) retagged to ``TODO(v0.26+)`` with clearer wording.
- `src/adapters/claude_code.py:85, 288` and `src/adapters/cursor.py:81` — ``TODO(phase-3)`` markers retagged to ``TODO(v0.27+)``. Version-based markers age more gracefully than phase numbers because anyone can compare against the current release.

### Tests
- **35 new tests** total (6 reset + 16 prune + 4 dry-run + 5 logs + 4 file-sink). Full suite: **2,427 / 2,427 + 7 expected skips** (was 2,396 in v0.25.1; +31 on the visible delta because some prune tests are parametrized).
- Legacy "stub exits 1" tests in ``tests/test_cli_prune_reset.py``, ``tests/test_cli_smoke.py``, and ``tests/test_cli_execute.py`` rewritten to assert the new behavior (or replaced by the comprehensive per-command suites above).

### Operator notes
- Once installed, ``/autodev reset --hard`` from inside Claude Code now does what it advertises — the unity recovery flow is unblocked.
- ``.autodev/sessions/`` will start accumulating one subdir per ``execute``/``plan``/``resume`` run. Use ``autodev prune --older-than 30d`` to GC.

## [0.25.1] - 2026-05-11

Targeted fix release for four orchestrator bugs surfaced by a long-running real-world execute run on a Unity-scale C++ repo. All four cascade-failed Phase 2: a Phase 2 stuck-error tripped a worktree-cleanup that wiped sibling per-task worktrees, downstream tasks then dispatched against HEAD where their prerequisites didn't exist, agent output blobs leaked into path arguments, and the resume-loop burned the retry budget in milliseconds. Each bug is fixed in isolation with regression coverage; full test suite passes (2,396 / 2,396 + 7 expected skips).

### Fixed (Bug #1 — `worktree.cleanup_all` parent-wipe)
- **`WorktreeManager.cleanup_all` no longer treats the per-task `tasks/` parent directory as a worktree label.** Regression introduced when `create_per_task` adopted the `tasks/<task_id>` hierarchy without updating `cleanup_all`: the cleanup `iterdir()` walked the tournament dir, found `tasks/` alongside impl labels `a`/`b`/`ab`, fed `"tasks"` through `remove(label="tasks", force=True)` and ultimately `shutil.rmtree(<dir>/tasks)`, destroying every per-task worktree in one call (including in-flight ones with un-integrated patches). The unity run's Phase 2 cascade was the surface symptom; the log line `worktree.force_removed component=worktree path=…/execute_worktrees/tasks` was the smoking gun. Fix: `cleanup_all` filters `tasks/` out of the label sweep and iterates `tasks_dir.iterdir()` through `remove_per_task` instead. Defensive guards added in `remove()` and `_force_remove()` reject the reserved `tasks` path so future regressions can't reintroduce the bug. (`src/orchestrator/worktree.py`)

### Fixed (Bug #2 — persistent integration via commit-per-task)
- **`apply_patch_to_main` can now commit the patch atomically with the apply.** A new optional `commit_message: str | None = None` parameter routes the apply through `git apply --index` (stages exactly the diff's hunks) followed by `git commit -m <message>`. `None` preserves v0.25.0 working-tree-only behavior used by impl tournaments. `_apply_with_conflict_escalation` in `execute_phase.py` passes `commit_message=f"autodev: task {task.id} ({task.title})"` for both clean and `--3way` paths so each task lands as a separate commit on the main branch. Subsequent `create_per_task` calls (which default `base_ref="HEAD"`) now see prior tasks' changes, unlocking cross-task dependencies. Without this fix, Phase 2's later tasks were dispatched against the original HEAD where Phase 2.1's `ContextTimerQueryStateGLES` and `debugGroupDepth` symbols did not exist. (`src/orchestrator/worktree.py`, `src/orchestrator/worktree_pool.py`, `src/orchestrator/execute_phase.py`)

### Fixed (Bug #3 — agent-output leak into path arguments)
- **`extract_files_from_diff` rejects malformed diff "paths" before they reach QA gates.** When a developer agent emits a JSON-escaped multi-line code listing into the `diff` field of `.autodev/responses/{task_id}-{role}.json`, the unified-diff parser at `src/adapters/git_utils.py` treats the whole blob as one line and `+++ b/<path>` extracted a 4000+ char "path" containing literal `\n` escapes. That string flowed through `execute_phase.py` into `_iter_files` helpers in `qa/secretscan.py`, `qa/hallucination_guard.py`, and `qa/code_size.py`, where `resolved.is_file()` / `resolved.exists()` raised `OSError: [Errno 63] File name too long` and the worker died with `worker_exception` (the unity run had 10 tasks blocked this way across phases 0/1/2). Two-layer fix: (1) `extract_files_from_diff` now rejects paths longer than 255 bytes (POSIX `NAME_MAX`) or containing `\n` / `\\n` / `\x00`, logging the rejected prefix; (2) the three QA `_iter_files` helpers wrap `is_file()`/`exists()` in `try/except (OSError, ValueError)` so any future leak skips the path instead of crashing the worker.

### Fixed (Bug #4 — retry backoff persistence across resume)
- **Retry attempts now respect a minimum interval (`qa_retry_min_interval_s`, default `30.0` s).** v0.25.0 had no backoff anywhere in the retry loop — a task wedged at `retry_count=N` after `autodev resume` burned through retries N+1, N+2, … within milliseconds (the unity run observed sub-second ledger sequences seqs 349-356 for task 2.5). Fix: new `Task.last_retry_at: str | None` field, persisted via `PlanManager.mark_task_retry` and restored on ledger replay; new `_enforce_retry_backoff` helper in `execute_phase.py` sleeps for `min_interval_s - elapsed` at the top of `_try_retry_or_escalate` when the prior retry was within the interval. Helper takes injectable `now` and `sleep` so tests run instantly. Set `qa_retry_min_interval_s=0.0` to disable the guard entirely (v0.25.0 behavior; not recommended). (`src/state/schemas.py`, `src/state/plan_manager.py`, `src/orchestrator/execute_phase.py`, `src/config/schema.py`, `src/config/defaults.py`)

### Added (config schema)
- `AutodevConfig.qa_retry_min_interval_s: float = 30.0` — Bug #4 retry-interval floor.

### Added (state schema)
- `Task.last_retry_at: str | None = None` — UTC ISO timestamp of the most recent `mark_task_retry`. Older ledgers (pre-v0.25.1) restore as `None`, backward-compatible default.

### Tests
- 35 new tests across 4 new/extended test files:
  - `tests/test_orchestrator_worktree.py` — 7 new (3 for Bug #1, 4 for Bug #2). Confirmed RED before fix, GREEN after.
  - `tests/test_orchestrator_retry_backoff.py` — 8 new (Bug #4).
  - `tests/qa/test_iter_files_long_path.py` — 9 new (Bug #3 Layer 3 guards).
  - `tests/adapters/test_git_utils_extract_files_sanitize.py` — 7 new (Bug #3 Layer 2 sanitizer).
  - `tests/test_orchestrator_conflict_escalation.py` — `FakeWorktreeMgr.apply_patch_to_main` signature updated to accept `commit_message`.

### Migration / operator notes
- The new commit-per-task behavior means every successful task on the execute path produces a commit on the main branch. Operators who relied on the prior "everything in one big uncommitted working tree" behavior should review their post-run review flow. The legacy behavior is preserved for impl tournaments (which don't pass `commit_message`).
- `qa_retry_min_interval_s` defaults to 30 s. Set to a lower value in `.autodev/config.json` if you observe legitimate fast retries (e.g., transient adapter flakes); the value applies uniformly across all retry paths.
- No on-disk schema migration is required; old plans deserialize with `last_retry_at=None` and the backoff guard is a no-op until the next `mark_task_retry` stamps it.

## [0.25.0] - 2026-05-10

### Added (planner substrate)
- **Repo file/symbol index for planner candidate lookup.** New `src/state/file_index.py` builds a sqlite-FTS5 index (`.autodev/index.db`) of every tracked file and its top-level symbols (functions, classes, methods, namespaces, structs). Built at `autodev init` (synchronous for small/medium repos, background subprocess on huge repos detected via `runtime.repo_probe.RepoCapacity.is_huge`); refreshed incrementally on every `autodev execute`/`plan`/`resume` via `git diff --name-only <last_indexed_sha>..HEAD` (mtime fallback for non-git repos). WAL mode lets per-task worktrees query the index concurrently without contention. `.autodev/index.db.lock` PID file enforces single-writer; `.autodev/index.state.json` tracks `last_indexed_sha` atomically for recovery from sqlite corruption mid-build.
- **Architect candidate-file injection.** `orchestrator/plan_phase.py` now queries the index with the spec text (`IndexQuery.get_candidates_for_spec`) and prepends a CANDIDATE_FILES block to the architect's `DelegationEnvelope.context`. The architect prompt (`src/agents/prompts/architect.md`) gained a `## CANDIDATE FILES` section instructing it to prefer indexed paths over invented ones and to use the v0.24.3 `[new]` prefix for genuinely-new files. Cuts hallucinated path retries on Unity-scale repos. Smoke benchmark on AutoDev itself: 494 files / 4124 symbols indexed in 378 ms; spec-keyed candidate lookup returns the right symbols + files.
- **Per-language symbol extractors.** `src/state/language_extractors/` exposes a `LanguageExtractor` Protocol with concrete extractors for Python (`ast.parse` + walk), C++ (reuses `qa.cpp_symbols.extract_declarations` + tree-sitter walk when available), TypeScript/JavaScript (tree-sitter-typescript when installed, regex fallback otherwise), and a regex catch-all for unknown languages.
- **`autodev doctor` Index section.** Reports `path`, `file_count`, `symbol_count`, `last_indexed_sha`, `last_indexed_at`, `index_version`. Surfaces missing-index state with a one-line operator action.
- **`autodev status` Index row.** One-line snapshot in the existing summary table.
- **`autodev init --rebuild-index` flag.** Forces a full index rebuild without overwriting other scaffolding.
- **v0.24.3 fuzzy-suggestion upgrade.** `file_existence_validator._RepoFileSnapshot.closest()` now prefers `IndexQuery.search_files(...)` when the index is available, falling back to the v0.24.3 difflib-over-git-lsfiles path otherwise. Higher-quality "did you mean" hints in the architect-retry envelope.

### Added (config schema)
- `AutodevConfig.index_enabled: bool = True`
- `AutodevConfig.index_path: str = ".autodev/index.db"`
- `AutodevConfig.index_languages: list[str] | None = None` (None = auto-detect: py, cpp, ts)
- `AutodevConfig.index_full_rebuild_threshold_files: int = 5000` (re-index from scratch when more files changed than this)
- `AutodevConfig.index_huge_repo_async_init: bool = True`

### Added (runtime)
- `runtime.repo_probe.iter_repo_files(cwd, extensions=None) -> Iterator[Path]` — public iterator over tracked files (`git ls-files` fast-path) or walked files (`os.walk` fallback). Reuses the `qa.hallucination_guard` skip-dirs set.
- `state.paths.index_db_path(cwd) -> Path` — canonical resolution of the index db location.

### Added (optional dependency)
- `tree-sitter-typescript >= 0.21` lands in `[project.optional-dependencies] tree-sitter` (alongside the existing `tree-sitter-cpp`). Optional: regex fallback covers when not installed.

### Added (gitignore)
- `.autodev/index.db`, `.autodev/index.db-shm`, `.autodev/index.db-wal`, `.autodev/index.db.lock`, `.autodev/index.db.building`, `.autodev/index.state.json`, `.autodev/index-build.log` — all per-workspace, never checked in.

### Migration
- Workspaces created with v0.24.x have no `.autodev/index.db`; first `autodev execute`/`plan`/`resume` triggers a full build (synchronous on small/medium repos, async on huge). `autodev doctor` reports the missing-index state until the build completes.
- Kill switches: per-workspace via `cfg.index_enabled = False`; per-process via env var `AUTODEV_INDEX_DISABLED=1` (raises `IndexDisabledError`, caught and logged at the call site as a no-op).

### Tests
- 35 new tests across 8 new test files (`test_state_file_index_build.py`, `test_state_file_index_query.py`, `test_state_language_extractors.py`, `test_runtime_repo_probe_iter_files.py`, `test_orchestrator_plan_phase_with_index.py`, `test_cli_init_with_index.py`, `test_cli_execute_incremental_index.py`, `test_orchestrator_file_existence_validator_with_index.py`). `test_ts_extractor_with_treesitter` skips cleanly when the optional binding is absent.

### Notes on deferred items
- **Removal of `worktree_sparse_checkout_enabled` deferred to v0.26.0** (was scheduled for v0.25.0 per the v0.24.0 deprecation note). v0.25.0 stays scope-focused on the new index; the deprecated flag continues to work as in v0.24.x.
- The architect's behavior degrades gracefully when the index is absent or disabled — the candidate-files context value is an empty string and the prompt's "PREFER paths from this list" sentence becomes a no-op.
- LSP integration (clangd / pyright / tsserver) is the natural next step after this release once tree-sitter coverage proves insufficient. Not in scope for v0.25.0.

## [0.24.3] - 2026-05-10

### Fixed
- **Architect-emitted file paths are validated against the filesystem before delegation.** Previously, `parse_plan_markdown` accepted any string in a `Files:` line, including hallucinated paths that smashed directory prefixes together with C++ source content; the worker then died with `[Errno 63] File name too long` mid-task. New `src/orchestrator/file_existence_validator.py` walks every `Task.files`, `Task.extended_scope`, `Phase.edit_scope`, and `Plan.edit_scope` entry and raises `PathValidationError(reason="missing_on_disk")` on the first miss with a `difflib`-fuzzy "did you mean" suggestion sourced from `git ls-files`. The error flows into the existing v0.22.4 architect-retry envelope at `plan_phase.py:173-211`, so the architect self-corrects on the second pass. Validation is wired into both the first parse and the retry parse. No-ops on non-git trees / empty snapshots — without ground truth there's no basis to flag any path as missing.

### Added
- **`[new]` prefix on `Files:` lines marks paths that the task will create.** Parser strips the prefix and routes the path into a new `Task.files_new: list[str]` field; `validate_files_exist` skips those paths during existence checks. Lets create-new tasks coexist with strict path validation. Default empty list — back-compat with v0.24.2 `plan.json` on disk.

### Tests
- `tests/test_orchestrator_file_existence_validator.py` — 10 cases (happy path, missing file, fuzzy suggestion, `[new]` prefix, extended_scope, phase/plan edit_scope, snapshot caching).
- Extends `tests/test_orchestrator_plan_phase.py` and `tests/test_orchestrator_plan_parser.py` with retry-hint and prefix-parsing assertions.

## [0.24.2] - 2026-05-10

### Changed
- **`/autodev` slash command is now a full CLI passthrough.** Every registered `autodev` subcommand (`resume`, `status`, `doctor`, `metrics regex-timeouts`, `metrics export-corpus`, `tournament`, `prune`, `reset`, `secretscan`, `plugins`, `logs`, `init`, `plan`, `execute`) is reachable directly via `/autodev <subcommand> [args]` from inside Claude Code; help/version flags (`--help`, `-h`, `--version`) too. Pre-0.24.2 the slash command only chained `plan` → `execute` and parsed any other input as a feature description, so `/autodev resume` re-planned from scratch instead of resuming. The legacy intent flows (`/autodev <feature>` one-shot and `/autodev --review <feature>` checkpointed) are unchanged. Template rendered by `adapters.inline_config.render_claude_slash_command`; freshly-`init`-ed workspaces pick up the new template automatically. Existing workspaces should run `autodev init --inline --force` to refresh `.claude/commands/autodev.md`.

## [0.24.1] - 2026-05-10

### Fixed
- **npm release pipeline.** `npm/scripts/bundle-wheel.js` now syncs `npm/package.json` `version` from `src/_version.py` at build time. The publish workflow had failed at the npm step for every release v0.22.1 → v0.24.0 because the npm manifest was hardcoded at `0.22.0`; PyPI publishes succeeded but npm rejected with "cannot publish over the previously published versions: 0.22.0". Future tag pushes will produce matching PyPI + npm releases automatically.

## [0.22.4] - 2026-05-10

### Added
- **B4 — Structured path normalization pipeline.** New `src/orchestrator/path_validator.py` exposes `normalize_path(raw, allow_glob=True)` (NFC unicode → strip whitespace → strip outer quotes/backticks → strip trailing punctuation → strip `./` → reject control chars / empties / parent segments / absolute paths → `posixpath.normpath` → strip trailing `/`) and `validate_paths_batch(paths)` returning a `(normalized, errors)` partition. Errors are `PathValidationError(raw, reason, suggestion)` — machine-readable for the architect-retry envelope. The `dag.py` diagnostic helper now delegates to this module so error messages and the validator agree on the canonical form. The `plan_phase.py` retry loop catches `PathValidationError` and `pydantic.ValidationError` (in addition to the legacy `PlanParseError`) so malformed paths trigger architect self-correction with explicit format rules instead of wedging at execute time.

### Tests
- `tests/test_orchestrator_path_validator.py` — 32 cases covering well-formed inputs, all rejection reasons, glob handling, batch partitioning.
- `tests/test_orchestrator_path_validator_nfc.py` — NFC-vs-NFD canonicalization.

## [0.22.3] - 2026-05-10

### Fixed (atomicity + observability)
- **B3 — Atomic evidence ↔ ledger via `attempt_started` marker + `reconcile_evidence_vs_ledger`.** Pre-flight ledger marker emitted before every developer dispatch; resume-time reconciler walks `evidence/*-developer.json` and auto-promotes orphans (success=true with no `coded` op AND a matching marker) to `coded`. Discrepancies (no marker / terminal status / promote-failed) emit a `reconcile_evidence` audit op for operator review. Closes the D-3 evidence-loss gap from the 2026-05-09 Unity stall. Wired into `run_execute_phase` BEFORE `reap_orphans` so genuine completed work is preserved instead of reverted.

### Added (operator knobs)
- **C2 (partial) — Secretscan tunables.** Three new fields on `QAGatesConfig` for huge-repo huge-fixture noise suppression: `secretscan_ignore_paths` (gitignore-style globs that bypass scan; e.g. `["Tests/**", "*.unity.meta"]`); `secretscan_entropy_threshold` (override the legacy 4.5 default — 4.8 suppresses 32-char hex GUID FPs); `secretscan_min_entropy_length` (override the legacy 20 floor — 32 filters short hex while preserving real-world keys). Composes with the existing v0.19.0 `.autodev/secretscan-allow` allowlist + per-extension entropy curve. Diff-mode default-on lands fully in v0.23.0.
- **C6 — `regex_timeout` ledger op telemetry.** Watchdog (v0.22.1 A1) now emits an audit ledger entry with `{path, timeout_s, gate}` whenever a per-file regex scan hits the timeout. Best-effort: failure to emit is logged at debug, never blocks the gate. Foundation for `autodev metrics regex-timeouts` in v0.23.0.

### Added (ledger ops)
- `attempt_started` — pre-flight developer-dispatch marker.
- `reconcile_evidence` — resume-time orphan-evidence summary.
- `reap_orphans` — resume-time orphan-task summary (paired with v0.22.2 B1).
- `regex_timeout` — QA gate watchdog event.

All four are audit-only (NOT mutated by `_apply_op`).

## [0.24.0] - 2026-05-10

### Added (streaming + observability)
- **D1 — Streaming ledger reader.** New `state.ledger.stream_entries(cwd)` yields `LedgerEntry` objects without buffering — useful for forensic walks on multi-MB ledgers (Unity's was 2.97 MB / 140 entries; production runs may push higher). The chain integrity invariants (seq monotonicity, `prev_hash` linkage, `self_hash` recompute) are validated incrementally; corruption raises `LedgerCorruptError` from either the streaming or the buffered surface. The legacy `read_entries` is now a thin `list(stream_entries(cwd))` wrapper.
- **D2 — `qa.sandbox.run_sandboxed` for hard wall-clock isolation.** Asyncio thread watchdogs (v0.22.1 A1) cannot kill CPU-bound workers in CPython; the sandbox spawns a one-shot `multiprocessing.Process` (forkserver context where available) that the parent can `terminate()` / `kill()` on timeout. Optional `on_timeout` callback constructs synthetic fallback values so callers can produce gate-specific results. Opt-in per call site for v0.24.0; the in-process watchdog remains the first line of defense.
- **D3 — `autodev metrics regex-timeouts` CLI subcommand.** Aggregates v0.22.3 C6 ledger telemetry into a top-N table (or JSONL for piping) so operators can identify recurring offenders before they cascade.
- **D4 — `autodev metrics export-corpus` CLI subcommand.** Redacted JSONL export of impl-tournament + phase-review outcomes (text fields → SHA256 hashes) for ADR-0042's longitudinal Phase 6 corpus. Each row carries seq + op + task_id + per-op metadata; designed to be shareable across teams without leaking source.
- **D5 — `RepoCapacity` shape signals.** Three new fields populated by `probe_repo`:
  - `avg_file_size_bytes` (int) — aggregate `total_bytes // file_count`.
  - `largest_dir` (str) — repo-relative path of the directory containing the most files.
  - `largest_dir_file_count` (int) — file count in the busiest directory.
  Useful for sparse-checkout pattern derivation and future shape-aware QA gate tuning. Default `0`/`""` preserves back-compat for callers constructing `RepoCapacity` directly.
- **D6 — Anti-fragility playbook (`docs/anti_fragility_playbook.md`).** Field guide mapping symptoms to ledger ops + log events + canonical operator actions. Documents the recovery recipe for the abandoned Unity workspace (now end-to-end automatic with v0.22.1 + v0.22.2 + v0.22.3 + v0.23.0).

### Notes on deferred items
- B4 (full path normalization pipeline) shipped in v0.22.4 ahead of v0.23.0.
- C2 (full default ignore_paths set + diff-mode default) — operator-tunable knobs landed in v0.22.3; opinionated defaults remain deferred. The anti-fragility playbook documents the tunable surface; defaults will land alongside Phase 6 corpus signals proving common fixture patterns.
- ADR-0042 ("Code the Transforms" deferred to v0.23.0+) trigger criteria remain unchanged — v0.24.0 D4 ships the corpus-collection scaffolding so the longitudinal data accumulates passively while operators run AutoDev normally.

### Deprecation
- `worktree_sparse_checkout_enabled` flag (introduced v0.17.0, superseded by `worktree_huge_repo_mode` in v0.23.0) — emits no warning yet but is scheduled for removal in v0.25.0. Operators should migrate to `worktree_huge_repo_mode` semantics.

## [0.23.0] - 2026-05-10

### Added (huge-repo mode)
- **C1 — `worktree_huge_repo_mode` first-class config + sparse-by-default.** New `AutodevConfig.worktree_huge_repo_mode: Literal["auto", "on", "off"]` (default `"auto"`) controls cascading huge-repo defaults. When huge mode resolves on, per-task worktrees become sparse-checkout (was opt-in via `worktree_sparse_checkout_enabled`); the worktree-add timeout extends to `worktree_huge_create_timeout_s` (default 600 s); the warm-pool size shrinks to `worktree_huge_pool_size` (default 2). The legacy `worktree_sparse_checkout_enabled` flag remains as a deprecated alias for v0.23.0 (removal scheduled for v0.24.0). Resolves D-3's worktree commit-back delay on Unity-scale runs (~80-180 s full-checkout vs <10 s sparse).
- **C3 — CLI signal handlers + child-kill on cancel + lockfile PID recording.** SIGTERM/SIGHUP handlers in `cli/__init__.py` log the signal name and raise `SystemExit(128+signum)` so `finally` blocks (notably `plan_lock` release) run before exit; previously SIGTERM died silently. Claude Code adapter (`adapters/claude_code.py`) now kills its child `claude -p` process on parent `CancelledError` (in addition to the existing `TimeoutError` path), preventing leaked subprocesses. Lockfile (`state/lockfile.py`) records `<pid> <iso8601>` instead of 0 bytes; on stale-lock detection the recorded PID is checked with `os.kill(pid, 0)` — dead-PID locks are auto-overwritten on next acquire (with a warning), alive-PID locks log a structured warning before the 30 s timeout fires.
- **C4 — Plan-tournament huge-repo fast-path.** Unity-scale plan tournament burned 80 min on the multi-branch dispatch (3 branches × 3-5 passes × 5 judges per branch). When `is_huge` resolves true and `cfg.tournaments.plan.huge_repo_overrides_disabled` is `False` (default), `orchestrator.plan_phase` falls back to a single-branch tournament — plan completes in <20 min instead. Operators with bigger compute budgets opt out by setting the override field to `True`.
- **C5 — Explorer max_turns 2x on huge repos.** The explorer's job is to enumerate the codebase; on huge repos (Unity: 358K files) the default 3 turns is insufficient. Wired in `orchestrator/plan_phase.py:_delegate` against `orch._repo_capacity.is_huge`.
- **C6 — `regex_timeout` ledger op telemetry.** Watchdog (v0.22.1 A1) emits an audit ledger entry on per-file regex timeout; `autodev metrics regex-timeouts` planned for v0.24.0 D3.
- **C7 — Operator documentation.** New `docs/huge_repo_guide.md` covers the cascading defaults, override knobs, and recovery recipe. ADR-0043 documents the design choice (`auto`/`on`/`off` mode, no global toggle, per-subsystem escape hatches).

### Added (config schema fields)
- `AutodevConfig.worktree_huge_repo_mode` (Literal `"auto"|"on"|"off"`).
- `AutodevConfig.worktree_huge_create_timeout_s` (int, default 600, bounded [60, 3600]).
- `AutodevConfig.worktree_huge_pool_size` (int, default 2, bounded [0, 8]).
- `TournamentPhaseConfig.huge_repo_overrides_disabled` (bool, default False).

### Deferred
- **B4 — full path normalization pipeline.** Shipped in v0.22.4 ahead of v0.23.0.
- **C2 (full)** — default `secretscan_ignore_paths` set + diff-mode-default. v0.22.3 shipped the operator-tunable knobs; the opinionated defaults will land in v0.24.0 alongside the streaming-parser / sandboxing work.
- **D1-D6** — entire v0.24.0 streaming-parser / sandboxing / corpus surface.

## [0.22.4] - 2026-05-10

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
