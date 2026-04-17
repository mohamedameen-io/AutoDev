# ADR-003: Borda Count Tournament Algorithm

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** tournament, borda-count, self-refinement, judges, convergence, autoreason
**Related ADRs:** ADR-001 (each tournament role is a stateless adapter call), ADR-002 (tournament results recorded in the ledger)

## Context

AutoDev uses a tournament-based self-refinement loop to improve both plans and implementations. Each tournament round (called a "pass") produces three candidate versions of the content:

- **Version A** (incumbent): the current best version
- **Version B** (revision): a fresh alternative produced by an architect agent after receiving critic feedback
- **Version AB** (synthesis): a merge of A and B produced by a synthesizer agent

After generating all three versions, N independent judge agents each rank them from best to worst. The tournament must aggregate these rankings into a single winner, handle disagreement among judges, tolerate judge failures, and decide when the content has converged (stopped improving).

The pass structure per round is: CRITIC -> ARCHITECT_B -> SYNTHESIZER -> N parallel JUDGES -> aggregation. The tournament runs for up to `max_rounds` passes and converges when the incumbent (A) wins for `convergence_k` consecutive passes.

This algorithm is the core quality mechanism in AutoDev -- it determines whether plans and implementations improve or stagnate. The choice of aggregation algorithm directly affects robustness to noisy judges, computational cost, and alignment with the autoreason research that inspired AutoDev.

## Options Considered

### Option A: Elo Rating System

**Description:**
Treat each tournament version as a player in a rating system. Maintain Elo ratings across rounds. Each judge's ranking produces pairwise comparison results (A>B, A>AB, B>AB, etc.), which update the ratings. The version with the highest rating after N judge responses wins the round.

**Pros:**
- Well-understood rating system with decades of history in competitive ranking
- Naturally handles transitive preference (if A > B and B > C, A's rating reflects superiority over C)
- Ratings carry across rounds, potentially enabling faster convergence
- Rich ecosystem of tools and analysis techniques

**Cons:**
- Requires calibration of the K-factor (update magnitude); too high creates instability, too low makes the system sluggish
- Carrying ratings across rounds couples the rounds together, breaking the independence that makes stateless subprocess invocations clean
- Pairwise decomposition of a 3-way ranking creates 3 comparisons, each updating ratings differently -- the order of updates matters (non-commutative)
- Overkill for 3 candidates per round; Elo shines with many candidates over many games
- A judge failure removes multiple pairwise comparisons, making the impact harder to reason about
- Does not align with the autoreason research that AutoDev is ported from

### Option B: Single Judge Majority Vote

**Description:**
Use a single judge per round. The judge picks the winner (A, B, or AB). No aggregation needed. If the judge fails, retry once or default to the incumbent.

**Pros:**
- Simplest possible aggregation: the judge's answer is the answer
- Lowest cost: one judge call per round instead of N
- No aggregation logic, no tiebreak rules
- Fastest per-round execution

**Cons:**
- Extremely vulnerable to judge noise: a single hallucinated or confused ranking changes the outcome
- No way to detect or mitigate judge failure except retry (which doubles cost)
- No signal about confidence: a split 2-1 vote among 3 judges tells you something; a single vote tells you nothing
- No graceful degradation: judge failure means the round has zero signal
- Does not align with autoreason's N-judge design

### Option C: Borda Count with N Parallel Judges

**Description:**
Each of N judges independently ranks all 3 versions (A, B, AB) from best to worst. Borda points are assigned: 1st place = 2 points, 2nd = 1 point, 3rd = 0 points. Points are summed across all judges. The version with the highest total wins. On ties, conservative tiebreak gives priority to the incumbent (A). Judges that fail to parse are counted as invalid and excluded from the tally (graceful degradation). The presentation order of versions is randomized per judge to mitigate position bias.

**Pros:**
- Robust noise reduction: N independent rankings smooth out individual judge errors
- Graceful degradation: a failed or unparseable judge simply reduces `valid_judges` by 1; the remaining judges still produce a valid result
- Conservative tiebreak: when scores are equal, the incumbent wins, preventing churn from marginal improvements
- Randomized presentation order eliminates position bias (each judge sees versions in a different shuffled order)
- Simple implementation: Borda scoring is a few lines of arithmetic with no iterative computation
- Directly aligned with the autoreason research paper's algorithm
- Property-testable: the score-sum invariant (`valid * n * (n+1) / 2`) can be verified by Hypothesis

**Cons:**
- N judge calls per round multiply the LLM cost by N (typically 3x for default `num_judges=3`)
- Borda count is susceptible to strategic voting (irrelevant in this context since judges are independent LLM calls, not self-interested agents)
- Convergence detection via streak-counting is heuristic: `convergence_k=2` may stop too early or too late for some tasks
- The 3-label system (A, B, AB) means only 6 possible ranking permutations; with 3 judges and limited permutations, ties are common (handled by tiebreak)

## Decision Drivers

- **Robustness:** Tolerance to noisy or failed judges without losing the entire round's signal
- **Simplicity:** Aggregation algorithm must be easy to implement, test, and reason about
- **Determinism:** Same rankings must produce the same winner (no random tiebreak)
- **Research Alignment:** Must match the autoreason paper's algorithm for validated quality guarantees
- **Graceful Degradation:** A judge crash or parse failure should not crash the tournament
- **LLM Cost Efficiency:** The cost multiplier (N judges) must be justified by quality improvement
- **Testability:** Must be fully testable offline with deterministic RNG

## Architecture Drivers Comparison

| Architecture Driver        | Option A: Elo Rating | Option B: Single Judge | Option C: Borda Count | Notes |
|----------------------------|----------------------|------------------------|-----------------------|-------|
| **Robustness**             | ⭐⭐⭐                | ⭐                     | ⭐⭐⭐⭐⭐               | A: moderate (smooths over rounds); B: single point of failure; C: N independent judges smooth per-round noise |
| **Simplicity**             | ⭐⭐                  | ⭐⭐⭐⭐⭐                | ⭐⭐⭐⭐                | A: K-factor tuning, pairwise decomposition; B: trivial; C: straightforward arithmetic |
| **Determinism**            | ⭐⭐⭐                | ⭐⭐⭐⭐⭐                | ⭐⭐⭐⭐⭐               | A: update order affects ratings; B: deterministic by definition; C: tiebreak rule is deterministic |
| **Research Alignment**     | ⭐                   | ⭐                     | ⭐⭐⭐⭐⭐               | Only C matches the autoreason paper's algorithm |
| **Graceful Degradation**   | ⭐⭐                  | ⭐                     | ⭐⭐⭐⭐⭐               | A: losing a judge loses multiple pairwise results; B: losing the judge loses everything; C: one fewer ranking, still valid |
| **LLM Cost**               | ⭐⭐                  | ⭐⭐⭐⭐⭐                | ⭐⭐⭐                  | A: N pairwise calls; B: 1 call; C: N calls (default N=3) |
| **Testability**            | ⭐⭐⭐                | ⭐⭐⭐⭐⭐                | ⭐⭐⭐⭐⭐               | A: requires rating state across rounds; B: trivial; C: fully testable with Hypothesis property tests |
| **Position Bias Mitigation** | ⭐⭐⭐              | ⭐⭐                    | ⭐⭐⭐⭐⭐               | C randomizes display order per judge; A/B do not address this |

## Decision Outcome

**Chosen Option:** Option C: Borda Count with N Parallel Judges

**Rationale:**
Borda count with N independent judges provides the best robustness-to-simplicity ratio. It is the only option that directly aligns with the autoreason research paper's validated algorithm, which is the theoretical foundation for AutoDev's self-refinement approach. The conservative tiebreak (incumbent A wins on ties) prevents churn from marginal improvements, which is critical for convergence stability.

The LLM cost multiplier (3x for default N=3) is the primary trade-off, but it is justified by three factors: (1) judge calls are parallelized under a semaphore, so wall-clock time is close to 1x; (2) the noise reduction from multiple judges prevents wasted cycles on bad refinement decisions; (3) subscription CLIs (the primary deployment target) are not per-token-metered.

**Key Factors:**
- Graceful degradation is a hard requirement: in a 3-judge tournament, if one judge returns unparseable output, the other two still produce a valid result. This property is verified by `test_judge_parse_failure_counts_invalid`.
- Randomized presentation order (`randomize_for_judge`) eliminates position bias, which is a known issue when LLMs evaluate multiple options presented in sequence.
- The Hypothesis property tests prove invariants that hold for any valid ranking input: score-sum conservation, winner-has-max-score, and valid-count-matches-non-none.
- The autoreason golden-fixture regression test (`test_autoreason_golden_pass_01_scores`) verifies bit-exact match with the reference implementation's scoring.

## Consequences

### Positive Consequences
- Tournaments are resilient to individual judge noise: even if 1 of 3 judges gives an inconsistent ranking, the majority signal prevails.
- Conservative tiebreak creates a natural convergence bias: marginal improvements don't displace the incumbent, preventing oscillation.
- Full transparency: every `PassResult` records per-judge rankings, display order, raw response text, and aggregate scores. This enables post-hoc analysis of judge quality and bias.
- The `TournamentArtifactStore` writes per-pass markdown files (version_a.md, critic.md, version_b.md, version_ab.md, result.json) and final_output.md + history.json, creating a complete human-readable audit trail.
- The generic `ContentHandler[T]` protocol means the same tournament engine drives both plan refinement (`T = str`) and implementation refinement (`T = ImplBundle`).

### Negative Consequences / Trade-offs
- Each pass costs (3 + N) LLM calls: 1 critic + 1 architect_b + 1 synthesizer + N judges. With N=3 and max_rounds=30, worst case is 180 calls. Convergence typically occurs in 3-8 passes, so effective cost is 18-48 calls.
- Borda count treats all rank positions as equally spaced (2, 1, 0 points), which may not reflect the judges' actual confidence gaps. A judge who thinks A is far better than B but B is only slightly better than AB gives the same points as a judge with uniform preferences.
- The `convergence_k=2` heuristic (converge after 2 consecutive A wins) is a fixed threshold. Some tasks might benefit from a higher k (more confidence) while others converge earlier.

### Neutral / Unknown Consequences
- The `author_temp` and `judge_temp` fields in `TournamentConfig` are informational only -- subscription CLIs do not expose temperature control. If future adapters support temperature, these values will become actionable.
- The 3-label system (A, B, AB) could be extended to include more candidate versions per round. The Borda aggregation already supports arbitrary label lists, as demonstrated by `test_five_way_aggregation`.

## Implementation Notes

**Files Affected:**
- `src/tournament/core.py` - `Tournament` class, `aggregate_rankings()`, `parse_ranking()`, `randomize_for_judge()`, `PassResult`, `TournamentConfig`
- `src/tournament/prompts.py` - System prompts for CRITIC, ARCHITECT_B, SYNTHESIZER, and JUDGE roles
- `src/tournament/state.py` - `TournamentArtifactStore` for per-pass and final artifact persistence
- `src/tournament/llm.py` - `AdapterLLMClient` that wraps `PlatformAdapter.execute()` into the `LLMClient` protocol

**Ledger/State Implications:**
- Tournament completion is recorded in the ledger via `plan_tournament_complete` and `impl_tournament_complete` ops (audit-only, no plan mutation)
- These ops are handled as no-ops in both `ledger._apply_op()` and `plan_manager._apply_for_load()`

**General Guidance:**
- Judge responses must contain a `RANKING: X, Y, Z` line to be parsed; responses without this are treated as invalid (None ranking)
- Randomized presentation order uses the tournament's deterministic RNG (`random.Random` seed), making artifact files byte-identical across runs with the same seed
- The concurrency semaphore (`self._sem`) caps parallel judge subprocesses at `cfg.max_parallel_subprocesses` (default 3)
- When adding new candidate labels, update `aggregate_rankings`'s default `labels` parameter and the judge prompt template

## Evidence from Codebase

**Source References:**
- `src/tournament/core.py:140-166` - `aggregate_rankings()`: Borda scoring with configurable labels and conservative tiebreak. Points = `n - position` for each label in a judge's ranking.
- `src/tournament/core.py:118-133` - `randomize_for_judge()`: shuffles (A, B, AB) into random display order using the tournament's RNG; returns `order_map` mapping display position to canonical label
- `src/tournament/core.py:102-115` - `parse_ranking()`: extracts the last `RANKING:` line from judge output; requires at least 2 valid digits; returns None on failure
- `src/tournament/core.py:242-324` - `run_pass()`: CRITIC -> ARCHITECT_B -> SYNTHESIZER -> N judges -> Borda aggregation pipeline with coin-flip X/Y ordering for synthesizer
- `src/tournament/core.py:192-240` - `run()`: convergence loop with streak tracking; converges when `streak >= convergence_k`
- `src/tournament/core.py:326-374` - `_run_judges()`: spawns N judges concurrently, maps their positional rankings back to canonical labels via `order_map`
- `src/tournament/core.py:75-84` - `TournamentConfig`: `num_judges=3`, `convergence_k=2`, `max_rounds=30`, `conservative_tiebreak=True`

**Test Coverage:**
- `tests/test_tournament_core.py::test_convergence_all_A_in_two_passes` - When judges always favor A, tournament converges in exactly 2 passes (k=2)
- `tests/test_tournament_core.py::test_ab_always_wins_hits_cap` - When AB always wins, streak never forms; tournament runs to max_rounds
- `tests/test_tournament_core.py::test_alternating_then_settles` - B, AB, A, A sequence converges correctly at pass 4
- `tests/test_tournament_core.py::test_hash_changes_only_on_incumbent_change` - Hashes differ iff incumbent changed
- `tests/test_tournament_core.py::test_deterministic_artifacts_with_same_seed` - Same RNG seed produces byte-identical artifacts
- `tests/test_tournament_core.py::test_judge_parse_failure_counts_invalid` - 2 of 3 judges fail to parse; valid_judges=1; remaining judge's vote decides
- `tests/test_tournament_core.py::test_call_counts_per_pass` - Verifies exactly 1 critic + 1 architect_b + 1 synthesizer + N judges per pass
- `tests/test_tournament_borda_aggregation.py::test_tiebreak_A_beats_B_on_equal_scores` - Three-way tie resolved by conservative tiebreak to A
- `tests/test_tournament_borda_aggregation.py::test_all_none_rankings_fallback_to_tiebreak` - All judges invalid -> tiebreak picks A (zero scores)
- `tests/test_tournament_borda_aggregation.py::test_autoreason_golden_pass_01_scores` - Bit-exact match with autoreason reference implementation

**Property-Based Tests (Hypothesis):**
- `tests/test_tournament_borda_aggregation.py::test_score_sum_invariant` - Sum of Borda points = `valid * n * (n+1) / 2` for any ranking input (200 examples)
- `tests/test_tournament_borda_aggregation.py::test_winner_has_max_score_or_wins_tiebreak` - Winner always has max score; among ties, A wins if tied (200 examples)
- `tests/test_tournament_borda_aggregation.py::test_valid_count_matches_non_none` - valid_judges equals count of non-None rankings (200 examples)

## Related Design Documents

- [tournaments.md](../../docs/design_documentation/tournaments.md) - Full tournament subsystem design including pass structure, convergence semantics, and artifact layout
- [cost.md](../../docs/design_documentation/cost.md) - LLM cost model for tournament passes (N judges per round)
- [adapters.md](../../docs/design_documentation/adapters.md) - Each tournament role (critic, architect_b, synthesizer, judge) is a stateless adapter call (ADR-001)
- [architecture.md](../../docs/architecture.md) - Tournament engine's position in the orchestrator pipeline

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: Tournament convergence within 3-8 passes on typical tasks; no regressions against autoreason golden fixtures; Hypothesis property tests pass under extended settings
- [ ] Metrics to track: Average passes to convergence; judge parse failure rate; tiebreak frequency; per-pass wall-clock time with N=3 parallel judges

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
