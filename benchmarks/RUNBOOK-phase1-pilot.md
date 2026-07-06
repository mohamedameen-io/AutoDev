# RUNBOOK — Phase-1 SWE-bench-Lite pilot (Mac + Claude subscription)

The pilot is the **first operational deliverable** of the Phase-1 coarse
regression tripwire. Its job is **measurement, not a gate**:

1. **Which instances even run** — screen ~25–30 candidate SWE-bench-Lite instances
   for those that (a) build a per-instance **arm64** venv and (b) score via
   sb-cli. The survivors become the fixed ~15–20 slice.
2. **Throughput** — real wall-time per instance + quota-wait time → **sustainable
   instances/day** on the subscription. This number decides whether even a weekly
   coarse sweep is feasible.
3. **First baseline** — a healthy run over the survivors establishes
   `benchmarks/baselines/swebench-lite-phase1/<autodev_version>.json`.

> **This run consumes real Claude subscription quota and needs the network.** It is
> operator-gated — nothing in CI runs it. Expect it to park on the plan cap and
> resume (that is by design; see *Quota behaviour* below).

Governing decision: [ADR-0050](../docs/decisions/0050-phase1-mac-subscription-constraint.md).
Plan: [`thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md`](../thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md).
Glossary: [`benchmarks/CONTEXT.md`](CONTEXT.md).

---

## 0. Prerequisites

- macOS on Apple Silicon (arm64), on the machine signed into the personal Claude
  **subscription** (`autodev` on `PATH`, `claude` CLI authenticated). **No API
  keys** — quota, not dollars, is the wall.
- `git`, Python 3.11+, and `uv` (repo tooling).
- Network access (for `git clone` of the instance repos and for sb-cli).

## 1. Install sb-cli + get a free scoring token

`sb-cli` submits `predictions.jsonl` to the free SWE-bench cloud evaluator. The
token is **NOT an Anthropic key** — it is a free, email-issued SWE-bench token.

```bash
pip install sb-cli
sb-cli gen-api-key <your-email>          # emails you a token; confirm via the link
export SWEBENCH_API_KEY=<the-token>       # the sb-cli scorer reads this env var
```

Keep `SWEBENCH_API_KEY` exported in the shell you run the pilot from. If it is
unset, every instance is scored **ERROR** (a config failure — never a silent
FAIL).

## 2. Get the SWE-bench-Lite dataset (operator-supplied — two options)

The dataset loader (`benchmarks.datasets.swebench_lite`) is **network-optional**
and never a hard dependency. Choose ONE:

- **HuggingFace (online):**
  ```bash
  pip install datasets            # optional heavy dep; only needed for this path
  ```
  Then pass `--dataset swe-bench-lite` (the loader lazily pulls
  `princeton-nlp/SWE-bench_Lite`, split `test`).

- **Local JSONL (fully offline):** one instance object per line
  (`instance_id`, `repo`, `base_commit`, `problem_statement`, `test_patch`,
  `version`, `environment_setup_commit`, …). Pass it with `--instances-file
  path/to/instances.jsonl`.

You do **not** hand-pick instance IDs. The pilot's candidate selector loads the
whole dataset and applies a documented heuristic (below) to prefer
lighter-dependency / pure-python repos.

### Candidate-selection heuristic (`select_candidate_instances`)

- **De-duplicate** by `instance_id` (drops empty-id rows — they can't be scored).
- **Prefer pure-python repos**: instances whose `repo` matches a known
  heavy-native-build hint (`numpy`, `scipy`, `pandas`, `pillow`, `lxml`,
  `cryptography`, `torch`, … — see `HEAVY_DEP_REPO_HINTS` in
  `benchmarks/runner/pilot.py`) are **deprioritised**, because they are the ones
  most likely to fail an arm64 venv build and only ever solve *blind*. A tight
  `--count` drops those first.
- **Truncate** to `--count` (default ~30).

This is a coarse heuristic, not a guarantee — measuring which instances actually
build is the whole point of the pilot.

## 3. Smoke test (3 instances) — prove the pipeline end-to-end

Before an overnight run, prove one clean pass through solve → predictions → sb-cli
→ report on a tiny slice:

```bash
uv run python -m benchmarks.runner.pilot \
    --dataset swe-bench-lite \
    --count 3 \
    --workdir-root /tmp/bench-smoke \
    --out-dir results/pilot-smoke \
    --run-id pilot-smoke
```

(Or fully offline: `--instances-file path/to/3-instances.jsonl` in place of
`--dataset`.)

**Expect:** `results/pilot-smoke/pilot-report.json` + `pilot-summary.md`, each of
the 3 instances with a `status` (PASS/FAIL/ERROR), a `wall_time_s`, a
`quota_wait_time_s`, and a `blind` flag. A 3-instance run is below the gate's
`min_completed` floor, so the gate is **RED (insufficient-completed)** and **no
baseline is written** — that is correct anti-vacuity, not a failure of the smoke.

## 4. Full overnight, quota-aware pilot

```bash
uv run python -m benchmarks.runner.pilot \
    --dataset swe-bench-lite \
    --count 30 \
    --workdir-root /tmp/bench-pilot \
    --out-dir results/pilot-$(date +%Y%m%d) \
    --autodev-version "$(uv run python -c 'from _version import __version__; print(__version__)')-main-$(git rev-parse --short HEAD)"
```

Run it overnight. It solves **serially**, one instance at a time
(`tournaments.max_parallel_subprocesses = 1` is forced), and pauses/resumes around
quota aborts (see below). Leave the machine on and the shell open.

### Quota behaviour (why it looks like it's hanging)

When AutoDev trips the subscription cap (`usage_limit_hit` / `rate_limited` →
`InfrastructureCircuitOpenError`), the quota guard treats that instance as
**ERROR-until-complete** — it sleeps with backoff toward the next quota window (5
min, then doubling, capped at 1 h) and **re-runs the same instance**, up to
`--max-attempts` (default 6). A quota abort is **never** turned into a FAIL. If an
instance never clears the cap within the attempt cap it is recorded **ERROR
(quota-exhausted)**, still never FAIL. The report's `total_quota_wait_time_s` and
per-instance `quota_wait_time_s` are how you read real throughput.

## 5. Read the result → GO / NO-GO

Open `results/<run>/pilot-summary.md`. The key numbers:

- **`clean_count`** — instances that reached a real PASS/FAIL verdict *with
  self-repair on* (deps installed, not blind). This is the count that matters.
- **`blind_count`** — instances that solved blind (arm64 deps failed →
  `test_runner` off). High blind count = weak signal.
- **wall-time / quota-wait totals** — extrapolate to **sustainable
  instances/day** on the subscription.

Criteria:

- **GO (lock the slice + baseline):** **≥ ~15 clean instances** AND a full sweep
  **fits one overnight / quota window**. The report sets
  `recommend_lock = true` when `clean_count >= 15` (`GO_NOGO_MIN_CLEAN`). On a
  healthy run the gate is **GREEN (baseline-established)** and the pilot has
  already written the baseline JSON.
- **NO-GO (escalate the constraint):** arm64 dep failures are **pervasive** (most
  instances only blind-solve, `clean_count` well under 15). Do **not** lock a
  blind-only slice. Escalate per ADR-0050: accept a weaker blind-only signal, or
  move Phase-2-ward (local emulated x86 Docker, or a Linux x86_64 runner / Modal —
  the ADR-0050 unlock trigger).

## 6. What to commit (on a GO)

Commit **only** these two artifacts (never the per-instance workdirs, the cloned
repos, or the transient reports under `results/`):

1. **The baseline JSON** the healthy run established:
   `benchmarks/baselines/swebench-lite-phase1/<autodev_version>.json`.
2. **The locked slice** — pin the surviving instance IDs so future gate runs use
   the *same* fixed slice. Write them as a JSONL/`.txt` under
   `benchmarks/slices/swebench-lite-phase1.jsonl` (the exact instance records, so
   the slice is reproducible offline) and reference it via `--instances-file` on
   every subsequent gate run.

Record the measured **instances/day** throughput number in the commit message /
the Phase-1 exit notes — it is the number that decides the cadence (if any) of the
coarse sweep, and it is a Phase-1 exit criterion.

## 7. Fallback scoring (documented, not default)

If sb-cli is unavailable, the ADR-0050 fallback is a **local emulated x86 Docker**
`run_evaluation` (keyless, slow, heavy on a laptop). It is intentionally NOT the
default path — sb-cli offloads the x86 work cleanly. See ADR-0050 *Alternatives*.

---

### Appendix — one-shot smoke (offline, no dataset download)

To sanity-check wiring with zero network, hand the pilot a tiny local JSONL of
real Lite instances you already have, and score with sb-cli:

```bash
uv run python -m benchmarks.runner.pilot \
    --instances-file benchmarks/slices/smoke-3.jsonl \
    --count 3 \
    --workdir-root /tmp/bench-smoke \
    --out-dir results/pilot-smoke
```

The solve half still runs AutoDev (subscription quota), and the score half still
needs `SWEBENCH_API_KEY` + network — "offline" here means only that the *dataset
load* is offline.
