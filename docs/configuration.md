# Configuration

AutoDev's behaviour is controlled by `.autodev/config.json` in the repo
root. The file is created on `autodev init` from the shipped defaults
(see `src/config/defaults.py`); editing it lets operators tune
per-role models, per-role budgets, and per-task scaling without
touching code.

This document covers:

1. Per-role agent budgets (`max_turns`, `timeout_s`)
2. Per-task complexity multipliers
3. Per-(task, role) budget escalation on repeated `error_max_turns`
4. The override surface in `.autodev/config.json`

---

## 1. Per-role agent defaults

Each agent role has a configured `max_turns` (the number of agentic
loops the underlying CLI may execute before returning) and an
adapter-applied `timeout_s` (the wall-clock cap for one subprocess
call). Defaults live in `src/config/defaults.py:35-50`:

| Role                        | `max_turns` |
|-----------------------------|-------------|
| `architect`                 | 5           |
| `architect_b`               | 5           |
| `explorer`                  | 3           |
| `domain_expert`             | 3           |
| `developer`                 | 10          |
| `reviewer`                  | 3           |
| `test_engineer`             | 5           |
| `critic_sounding_board`     | 3           |
| `critic_drift_verifier`     | 3           |
| `docs`                      | 3           |
| `designer`                  | 3           |
| `critic_t`                  | 1           |
| `synthesizer`               | 1           |
| `judge`                     | 1           |

`timeout_s` defaults to `600` (10 min) inside the Claude Code adapter
and `_DEFAULT_DEVELOPER_TIMEOUT_S = 900` for developer dispatches in
`src/orchestrator/execute_phase.py`.

---

## 2. Per-task complexity multipliers

The architect tags every task with `- Complexity: simple|medium|complex`.
The orchestrator translates the bucket into a concrete `max_turns` /
`timeout_s` on the developer's `AgentInvocation`. Bucket defaults live
in `src/tournament/task_overrides.py:42-56`:

| Complexity | `max_turns` | `timeout_s` |
|------------|-------------|-------------|
| simple     | 10          | 600 (10 min) |
| medium     | 20          | 1200 (20 min) |
| complex    | 40          | 1800 (30 min) |

On large repos (`is_huge` returns `True` from
`runtime.repo_probe.probe_repo`) per-bucket multipliers in
`runtime.repo_probe._HUGE_BUCKET_MULTIPLIERS` (default
`simple 3.0×`, `medium 2.0×`, `complex 1.5×`) further scale the
`max_turns`. Override via `cfg.task_overrides.huge_repo_multipliers`.

---

## 3. Budget escalation policy

When the developer (or any role) hits `error_max_turns` on the same
`(task_id, role)` pair multiple times in a row, the orchestrator
escalates the per-call budget rather than retrying with the same
exhausted ceiling. See `src/orchestrator/budget_escalation.py`.

| Attempt | `max_turns` multiplier | `timeout_s` multiplier | Notes                                              |
|---------|------------------------|------------------------|----------------------------------------------------|
| 1       | 1× (configured base)   | 1× (configured base)   | First dispatch, no escalation                      |
| 2       | `ceil(prior × 1.5)`    | `prior × 1.25`         | Fires after 1× consecutive `error_max_turns`        |
| 3       | `ceil(prior × 2.0)`    | `prior × 1.5`          | Fires after 2× consecutive `error_max_turns`. Emits a `budget_escalation` ledger op. |
| 4       | hard fail              | hard fail              | Escalation exhausted; result subtype is `error_max_turns_escalation_exhausted`, error reads `budget escalation exhausted; consider raising defaults in '.autodev/config.json'`. |

**Important properties:**

* Escalation only fires for `error_max_turns`. Other failure subtypes
  (`timeout`, `parse_error`, `rate_limited`, `auth_failed`, etc.) do
  NOT trigger escalation — they reset the per-(task, role) counter so
  a recovered timeout doesn't bleed escalation state into the next
  attempt.
* The counter resets on success too — a successful run on
  `(task_id, role)` clears the prior escalation history.
* The counter is keyed on `(task_id, role)` — escalating the
  developer on task 1.1 does not affect the reviewer on task 1.1, nor
  the developer on task 1.2.
* The tracker is in-memory only — a fresh orchestrator instance (e.g.
  after `autodev resume`) starts the escalation ladder from zero. By
  design: persisting escalation state across sessions would risk
  permanently elevating budgets for tasks that succeed on their own
  the next attempt.
* Both `max_turns` and `timeout_s` are capped at sane ceilings
  (defaults: 100 turns, 3600s) so a misbehaving agent cannot acquire
  unbounded budget.

A `budget_escalation` ledger entry is emitted before the third-attempt
dispatch with payload:

```json
{
  "task_id": "1.1",
  "role": "developer",
  "prior_max_turns": 10,
  "new_max_turns": 20,
  "prior_timeout_s": 600,
  "new_timeout_s": 900,
  "attempt": 2
}
```

If the ledger op is unavailable for any reason, a structured
`logger.warning("orchestrator.budget_escalation", ...)` carries the
same payload so the breadcrumb is recoverable from log streams.

---

## 4. Overriding via `.autodev/config.json`

### Per-role agent overrides

The `agents` section keys on role name. Override `max_turns`,
`timeout_s`, `model`, or `effort` per role:

```json
{
  "agents": {
    "developer": {
      "max_turns": 20,
      "timeout_s": 1200
    },
    "reviewer": {
      "max_turns": 5
    }
  }
}
```

These overrides become the **base** values that the per-task
complexity multipliers and the budget-escalation ladder build on top
of. So setting `developer.max_turns = 20` plus a `complex` task means
the first dispatch uses 40 (the per-task complex bucket) — escalation
still applies on top of that if the agent burns through it.

### Per-task complexity multiplier overrides

```json
{
  "task_overrides": {
    "huge_repo_multipliers": {
      "simple": 4.0,
      "medium": 2.5,
      "complex": 1.5
    }
  }
}
```

These multiply the per-bucket `max_turns` only on repos detected as
huge. See `src/tournament/task_overrides.py` and
`src/runtime/repo_probe.py` for the exact resolution path.

### Budget-escalation ceilings

Operator-override surface — by default both ceilings come from
module-level constants in `src/orchestrator/budget_escalation.py`
(`DEFAULT_MAX_TURNS_CEILING = 100`,
`DEFAULT_TIMEOUT_S_CEILING = 3600`). To override, add a
`budget_escalation` block:

```json
{
  "budget_escalation": {
    "max_turns_ceiling": 200,
    "timeout_s_ceiling": 7200
  }
}
```

(The schema slot is read defensively — existing configs without the
section continue to work unchanged.)

### Review tournament (v0.32.0 — opt-in)

The v0.32.0 review tournament replaces the single-shot reviewer step
with an A/B/AB pipeline (developer patch + original review,
adversarial second-opinion, merge synthesis), then runs three FRESH
judges over the candidates via Borda count. "Do nothing" (A wins
twice in a row) is a first-class verdict so the loop converges on
"the original was fine, stop" instead of looping the developer.

The feature is **opt-in for v0.32.0**: the default is `false` for
one cycle so we can collect real-world telemetry before flipping the
default in v0.33.0.

To enable:

```json
{
  "tournaments": {
    "review_tournament_enabled": true,
    "review_num_judges": 3,
    "review_convergence_k": 2,
    "review_max_rounds": 5,
    "review_judge_roles": ["judge", "minimality_judge", "judge_explorer"]
  }
}
```

Fields:

* `review_tournament_enabled` (bool, default `false`) — master
  switch. When `true`, `execute_phase` swaps the legacy single-shot
  `delegate(..., "reviewer", ...)` call for an A/B/AB tournament.
  When `false`, behaviour is byte-identical to v0.31.x.
* `review_num_judges` (int, default `3`) — cohort size. Operators
  who pin `review_judge_roles` to a list get exactly that many
  judges (the list length wins).
* `review_convergence_k` (int, default `2`) — number of consecutive
  A wins required to declare "do nothing". Matches the autoreason
  published technique (NousResearch).
* `review_max_rounds` (int, default `5`) — hard cap on rounds
  before the runner escalates to `critic_sounding_board`. Keeps the
  per-task budget bounded.
* `review_judge_roles` (list[str] | null, default `null`) — cohort
  override. `null` uses the built-in 3-role cohort
  `["judge", "minimality_judge", "judge_explorer"]`.

Forensics:

* Evidence is written to
  `.autodev/evidence/{task_id}-review_tournament.json` carrying all
  three candidates + judge rankings + Borda scores.
* Ledger ops `review_tournament_started`, `review_tournament_judged`
  (per round), and `review_tournament_converged` /
  `review_tournament_escalated` mark the lifecycle.
* All v0.31.0 instrumentation (chunked envelope, MALFORMED parsing,
  `raw_response` capture, empty-result dump) is preserved by
  construction — each candidate inherits the same plumbing.

---

## See also

* `src/config/defaults.py` — the shipped defaults.
* `src/config/schema.py` — the Pydantic config schema.
* `src/orchestrator/budget_escalation.py` — escalation helper.
* `src/tournament/task_overrides.py` — per-task complexity resolvers.
