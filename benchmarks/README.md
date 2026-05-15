# AutoDev Real-Task Benchmark

A small, repeatable benchmark of "did the agent actually solve a real bug on
a real codebase?" — the fitness signal for any future external optimiser
(prompt-evolution, RL, etc.) and the objective measure of release quality.

Status: **v1, 5 synthetic tasks**. Lands with v0.32.0 (Phase 7).

---

## Why this exists

Unit tests answer "does the code I wrote behave the way I wrote it." The
synthetic E2E suite (Phase 6 of v0.31.0) answers "does autodev's plumbing
hold together end-to-end with fake binaries." Neither answers the question
that actually matters for shipping: **given a real broken codebase and a
real `claude` or `cursor` binary, does autodev produce a fix that makes the
test go green?**

Without that signal, every "this release is better than the last" claim is
opinion. With it, every release has a numeric pass-rate to compare.

---

## Scoring

**Primary (binary, per task):** apply the agent's diff to a clean copy of
the broken repo, run `test_command.sh`, exit code 0 = PASS. Anything else
= FAIL. There is no partial credit.

**Secondary (per task, recorded but not gating in v1):**

| Metric | Source |
|---|---|
| `wall_time_s` | sum of autodev subprocess wall-clock |
| `invocations` | number of `autodev` calls (init + plan + execute = 3) |
| `diff_size_lines` | additions + deletions in agent diff |
| `ground_truth_diff_size_lines` | same for `ground_truth.patch` |
| `diff_size_delta_lines` | agent − ground truth |
| `autodev_calls` | per-call `(args, exit_code, elapsed)` triples |

Token cost will land in v0.32.1 once the autodev ledger publishes a stable
cost field.

---

## Running it

```bash
# All 5 tasks, results to stdout
python -m benchmarks.runner.run_benchmark --task all

# Selected tasks, results to a file
python -m benchmarks.runner.run_benchmark \
    --task task_001_py_typeerror,task_003_py_slice \
    --autodev-version 0.32.0 \
    --platform claude_code \
    --output benchmarks/results/v0.32.0_$(date +%s).json

# Compare against a baseline
python -m benchmarks.runner.run_benchmark \
    --task all \
    --baseline benchmarks/results/v0.31.0_baseline.json \
    --output benchmarks/results/v0.32.0.json

# Just list tasks (no autodev calls)
python -m benchmarks.runner.run_benchmark --list
```

The runner expects the `autodev` CLI on `$PATH`. It does **not** install
autodev for you — the goal is to measure whatever release you have
installed, not whatever your dev tree currently builds.

### Cost

A full 5-task run costs roughly **$25–$50** in real Claude / Cursor API
tokens (subscription plans absorb most of this). It is **not** a per-PR
gate. Run it on release tags and during deliberate optimisation work.

### Timeouts

- Per-`autodev`-command wall-clock cap: 600 s (`--task-timeout`).
- Per-`test_command.sh` cap: 120 s (`--test-timeout`).
- A timed-out call aborts the task and marks it FAIL with `error="…timed out…"`.

---

## Adding a new task

Each task is a directory under `tasks/v1/` with exactly five things:

| Path | Purpose |
|---|---|
| `spec.md` | Bug description as the agent will read it. Plain English; no hints in code. |
| `repo/` | Pre-broken state. Must be ≤ ~200 LoC; runnable in isolation. |
| `ground_truth.patch` | Unified diff that, applied to `repo/`, makes the test pass. |
| `test_command.sh` | Regression test. **Must `cd "$(dirname "$0")/repo"` first**, then exit 0 with the fix and non-zero without. |
| `meta.json` | `{language, difficulty, expected_minutes, license, origin, description}` |

### Generating `ground_truth.patch`

```bash
diff -u repo/file.py.broken repo/file.py.fixed | sed 's|repo/file.py.broken|a/file.py|; s|repo/file.py.fixed|b/file.py|' > ground_truth.patch
```

### Validating a new task

```bash
# It must FAIL on the broken repo
bash tasks/v1/<your_task>/test_command.sh; echo "exit=$?"

# Apply the patch and confirm it passes
TMP=$(mktemp -d) && cp -r tasks/v1/<your_task>/repo "$TMP/repo" \
    && (cd "$TMP/repo" && patch -p1 < ../../tasks/v1/<your_task>/ground_truth.patch) \
    && bash tasks/v1/<your_task>/test_command.sh
```

The runner test suite (`tests/test_benchmark_runner.py::test_score_exact_match_passes`)
will then automatically validate that your `ground_truth.patch` makes
`test_command.sh` pass for every task in the corpus.

---

## Licensing policy (HARD rule)

**Every task in this benchmark MUST be one of:**

1. Fully synthetic (written for the benchmark; MIT-licensed via
   `tasks/v1/LICENSE`), OR
2. Derived from an open-source project under MIT / Apache-2.0 / BSD,
   with the `origin` field in `meta.json` pointing at the source URL
   and the upstream license file copied into the task directory.

**Customer code is NEVER permitted in this benchmark, under any
circumstances.** That includes anonymised customer code, "synthesised
from a customer pattern", or anything you can't relicense as MIT.

Tasks that violate the policy will be removed in PR review.

The v1 corpus is 100% synthetic — see each `meta.json` (`"origin":
"synthetic"`).

---

## Output schema

```json
{
  "benchmark_version": "v1",
  "autodev_version": "0.32.0",
  "timestamp": "2026-05-16T12:34:56+00:00",
  "platform": "claude_code",
  "summary": {"passed": 4, "failed": 1, "total": 5, "pass_rate": 0.8},
  "results": [
    {
      "task_id": "task_001_py_typeerror",
      "status": "PASS",
      "secondary": {
        "wall_time_s": 45.3,
        "invocations": 3,
        "diff_size_lines": 6,
        "ground_truth_diff_size_lines": 6,
        "diff_size_delta_lines": 0,
        "autodev_calls": [
          ["init", 0, 1.2],
          ["plan --spec /…/spec.md", 0, 18.4],
          ["execute", 0, 25.7]
        ]
      }
    },
    …
  ],
  "comparison": {
    "pass_rate": 0.8,
    "baseline_pass_rate": 1.0,
    "pass_rate_delta": -0.2,
    "regressed": true,
    "per_task": [
      {"task_id": "task_002_ts_nullcheck", "status": "FAIL",
       "baseline_status": "PASS", "regressed": true},
      …
    ]
  }
}
```

`comparison` is only present when `--baseline <path>` is supplied.

---

## CI integration

CI integration is **deferred to Phase 9** (release ceremony). When it lands
it will:

- Run on release-tag pushes only (NOT every PR — too expensive).
- Fail the release if `pass_rate` drops by more than 10% vs the prior
  release.
- Warn (not fail) if cost increases by more than 25%.

Until then the benchmark is invoked manually.

---

## v1 corpus

| ID | Lang | Difficulty | What's broken |
|---|---|---|---|
| `task_001_py_typeerror` | Python | easy | `format_price()` rejects `float` via an over-tight `isinstance` guard |
| `task_002_ts_nullcheck` | JavaScript | easy | `getUserById()` derefs DB result without a null check |
| `task_003_py_slice` | Python | easy | `parse_csv_rows()` skips header twice (off-by-one slice) |
| `task_004_go_defer` | Go | medium | `readLines()` leaks file descriptors — missing `defer file.Close()` |
| `task_005_py_perf` | Python | medium | `join_lines()` is O(n²) — re-joins the buffer every iteration |

All synthetic. All MIT-licensed via `tasks/v1/LICENSE`.
