# Cost

AutoDev runs on a **subscription account** (Claude Code or Cursor) — no API keys, no per-token billing. However, each agent invocation is a real LLM call that consumes your subscription quota. This document explains the cost model and how to reduce usage.

---

## Subscription-Based Cost Model

AutoDev does not use the Anthropic or OpenAI API directly. Every LLM call is a subprocess invocation of `claude -p` or `cursor agent --print` against your logged-in session. This means:

- **No API key required** — you need a Claude Code or Cursor subscription
- **No per-token charges** — usage counts against your subscription's rate limits
- **Rate limits apply** — heavy use (many parallel judges, long tournaments) can hit subscription rate limits; AutoDev retries with backoff on `TransientError`

---

## Cost Per Plan Estimate

The plan phase involves these calls:

| Step | Calls | Notes |
|---|---|---|
| explorer | 1 | haiku-tier, read-only |
| domain_expert | 1 | sonnet-tier |
| architect draft | 1 | opus-tier |
| plan tournament (per pass) | 3 + N judges | N=3 by default |
| critic_t gate | 1 | sonnet-tier |
| architect revision (if NEEDS_REVISION) | 1 | opus-tier, optional |

**With default settings** (3 judges, max 15 rounds, convergence_k=2):
- Minimum (converges in 2 rounds): 3 + (2 × 6) = **15 calls**
- Typical (converges in 4–6 rounds): 3 + (5 × 6) = **33 calls**
- Maximum (hits max_rounds): 3 + (15 × 6) = **93 calls**

---

## Cost Per Task Estimate

Each task in the execute phase involves:

| Step | Calls | Notes |
|---|---|---|
| developer | 1–4 | 1 initial + up to 3 QA retries |
| reviewer | 1 | sonnet-tier |
| test_engineer | 1 | sonnet-tier |
| impl tournament (per pass) | 3 + N judges | N=1 by default |

**With default settings** (1 judge, max 3 rounds, convergence_k=1):
- Minimum (converges in 1 round): 3 + (1 × 4) = **7 calls**
- Typical (converges in 2 rounds): 3 + (2 × 4) = **11 calls**
- Maximum (hits max_rounds): 3 + (3 × 4) = **15 calls**

---

## Tournament Cost Multiplier

The tournament is the primary cost driver. Here's how the multiplier works:

```
calls_per_pass = 3 (critic_t + architect_b + synthesizer) + num_judges

plan_tournament_max_cost  = calls_per_pass × max_rounds
                          = (3 + 3) × 15 = 90 calls

impl_tournament_max_cost  = calls_per_pass × max_rounds
                          = (3 + 1) × 3  = 12 calls per task
```

For a plan with 10 tasks:
- Plan phase: up to 90 calls (tournament) + 3 (explorer/domain_expert/architect) + 1 (critic_t) = **~94 calls**
- Execute phase: up to 12 calls/task × 10 tasks = **~120 calls**
- **Total maximum**: ~214 calls

In practice, tournaments converge early and QA retries are rare, so typical usage is 30–50% of the maximum.

---

## How to Reduce Costs

### Disable the implementation tournament

```bash
 autodev execute --no-impl-tournament
```

This skips the impl tournament entirely, saving up to 12 calls per task.

### Reduce tournament rounds

In `.autodev/config.json`:
```jsonc
"tournaments": {
  "plan": { "max_rounds": 5 },   // was 15
  "impl": { "max_rounds": 1 }    // was 3
}
```

### Reduce judge count

```jsonc
"tournaments": {
  "plan": { "num_judges": 1 },   // was 3
  "impl": { "num_judges": 1 }    // already 1
}
```

With 1 judge: 4 calls/pass × 5 rounds = 20 calls for plan tournament (vs. 90 max).

### Auto-disable for high-tier models

The autoreason paper shows tournament gains plateau above Haiku 4.5. If you're using sonnet or opus, you can skip tournaments:

```jsonc
"tournaments": {
  "auto_disable_for_models": ["opus", "sonnet"]
}
```

### Use haiku for more roles

```jsonc
"agents": {
  "developer":      {"model": "haiku"},
  "reviewer":      {"model": "haiku"},
  "test_engineer": {"model": "haiku"},
  "critic_t":      {"model": "haiku"},
  "architect_b":   {"model": "haiku"},
  "synthesizer":   {"model": "haiku"},
  "judge":         {"model": "haiku"}
}
```

This reduces quality but significantly reduces subscription usage.

### Set a cost budget

```jsonc
"guardrails": {
  "cost_budget_usd_per_plan": 5.00
}
```

The orchestrator warns before execution if projected calls exceed the budget. (Note: this is a call-count proxy, not actual dollar billing — subscription accounts don't have per-call pricing.)

---

## Cost Comparison: With vs. Without Tournaments

| Scenario | Plan calls | Per-task calls | 10-task total |
|---|---|---|---|
| No tournaments | ~3 | ~3 | ~33 |
| Impl tournament only (default) | ~3 | ~11 | ~113 |
| Plan + impl tournaments (default) | ~33 | ~11 | ~143 |
| Plan + impl tournaments (max) | ~93 | ~15 | ~243 |

---

## Monitoring Usage

Run `autodev status` to see how many tasks have completed and how many tournament passes were run:

```
 autodev status
```

Tournament artifacts are stored in `.autodev/tournaments/` with `history.json` files showing per-pass winners and elapsed time. Use `autodev prune --older-than 7d` to clean up old artifacts.
