# ADR-010: Conservative Tiebreak in Tournaments

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** tournament, tiebreak, borda-count, quality-monotonicity, autoreason
**Related ADRs:** ADR-008 (the tournament is a step within the deterministic FSM), ADR-009 (TournamentConfig and PassResult use strict Pydantic validation)

## Context

AutoDev's self-refinement tournament engine uses Borda count aggregation to determine a winner among three candidates in each pass: the incumbent (A), a revised version (B), and a synthesis of both (AB). N judges independently rank the three candidates, and Borda scoring assigns `n - position` points (3 for first, 2 for second, 1 for third). The candidate with the highest total score wins the pass.

The critical question arises when two or more candidates tie on Borda score. This happens frequently in practice: with 3 judges and 3 candidates, each judge contributing 6 total points, a three-way tie at 6-6-6 or a two-way tie at 7-7-4 is common, especially when judges produce `None` rankings (parse failure) that reduce the effective vote count.

The tiebreak policy directly affects quality trajectory: if ties default to the challenger (B or AB), the system makes changes even when judges are split, risking regression. If ties default to the incumbent (A), the system only changes when a challenger is strictly better, maintaining a "do no harm" quality floor.

This decision is configurable via `TournamentConfig.conservative_tiebreak: bool = True`, but the default and recommended setting is the subject of this ADR.

## Options Considered

### Option A: Random Tiebreak

**Description:**
When two or more candidates tie on Borda score, select the winner uniformly at random from the tied candidates. This treats all candidates as equally deserving when the judges cannot distinguish them.

**Pros:**
- Unbiased: no systematic preference for incumbents or challengers
- Simple to reason about: "if judges can't tell the difference, flip a coin"
- Introduces variety: over many tournament runs, ties resolve in different directions, exploring more of the solution space

**Cons:**
- Non-deterministic: the same judge rankings can produce different winners on different runs (depends on RNG state)
- Can regress quality: a random coin flip might replace a known-good incumbent with an untested challenger
- Violates quality monotonicity: the system's output quality can decrease across passes
- Makes test assertions harder: cannot predict the winner of a tied round without knowing the RNG seed
- In the degenerate case where all judges produce `None` (parse failure), a random winner is selected from `{A, B, AB}` with no signal whatsoever

### Option B: Always Challenger Wins (Aggressive Advancement)

**Description:**
When scores are tied, always pick the challenger (B) or synthesis (AB) over the incumbent (A). This policy maximizes the rate of change, assuming that "different is probably better."

**Pros:**
- Maximizes exploration: the tournament evolves the output aggressively
- Prevents stagnation: ties never cause the tournament to "do nothing"
- May converge faster to a global optimum by escaping local optima

**Cons:**
- Violates quality monotonicity: a tied score means judges found no clear advantage, yet the system changes anyway
- Increases cost: non-A wins reset the convergence streak to 0, requiring more passes to converge (and more LLM calls)
- Introduces churn: in the plan tournament, aggressive tiebreak can produce plans that oscillate between alternative structures without converging
- In the implementation tournament, replacing working code with an equally-rated alternative can introduce bugs not caught by the judges' ranking
- Makes `convergence_k` less meaningful: convergence requires `k` consecutive A wins, but ties that could have been A wins are given to challengers

### Option C: Always Incumbent Wins -- Conservative Tiebreak (Current Choice)

**Description:**
When scores are tied, the incumbent (A) wins. This is implemented via `tiebreak_winner="A"` in `aggregate_rankings()`. The tiebreak assigns priority 0 to "A" and higher priorities to other labels, so among tied scores, A sorts first. This is the default behavior, configurable via `TournamentConfig.conservative_tiebreak: bool = True`.

A challenger must be **strictly better** (higher Borda score) to displace the incumbent. In the degenerate case where all judges fail to parse (all `None` rankings), scores are {A: 0, B: 0, AB: 0} and A wins by tiebreak, preserving the known-good output.

**Pros:**
- Quality monotonicity: the output never regresses from a known-good state; changes happen only when a challenger demonstrates a clear advantage
- Cost efficient: ties count as A wins, contributing to the convergence streak; the tournament converges faster and uses fewer LLM calls
- Deterministic: same rankings always produce the same winner (no RNG in the aggregation)
- Robust to judge failures: if all judges return `None`, the incumbent is preserved rather than replaced by an untested alternative
- Aligned with the autoreason paper's philosophy: "the status quo is the safe default"
- Predictable for testing: Hypothesis property tests can assert "if A is among the tied labels, A wins"

**Cons:**
- May under-explore: genuine improvements that score equal to the incumbent are rejected
- Slight bias toward the initial proposal: the first version has a structural advantage across all rounds
- If the initial version is poor, it takes a clear majority of judges to displace it, potentially requiring more passes in cases where the initial quality is low

## Decision Drivers

- **Quality Monotonicity:** Never regress from a known-good state; output quality must be non-decreasing across tournament passes
- **LLM Cost Efficiency:** Ties should contribute to convergence, not extend the tournament with additional passes
- **Determinism:** Same judge rankings must always produce the same winner
- **Crash Safety:** In degenerate cases (all judges fail), the system must produce a valid, safe output
- **Testability:** Tiebreak behavior must be assertable in property-based tests without depending on RNG state

## Architecture Drivers Comparison

| Architecture Driver        | Option A: Random | Option B: Challenger Wins | Option C: Incumbent Wins (chosen) | Notes |
|----------------------------|-----------------|--------------------------|----------------------------------|-------|
| **Quality Monotonicity**   | ⭐⭐ (2/5)       | ⭐ (1/5)                  | ⭐⭐⭐⭐⭐ (5/5)                     | Only Option C guarantees output quality never decreases |
| **LLM Cost Efficiency**    | ⭐⭐⭐ (3/5)     | ⭐ (1/5)                  | ⭐⭐⭐⭐⭐ (5/5)                     | Ties count as A wins, accelerating convergence |
| **Determinism**            | ⭐⭐ (2/5)       | ⭐⭐⭐⭐⭐ (5/5)              | ⭐⭐⭐⭐⭐ (5/5)                     | Random depends on RNG state; B and C are deterministic |
| **Crash Safety**           | ⭐⭐ (2/5)       | ⭐ (1/5)                  | ⭐⭐⭐⭐⭐ (5/5)                     | All-None judges: Random picks arbitrarily; Challenger picks B; Incumbent preserves A |
| **Testability**            | ⭐⭐ (2/5)       | ⭐⭐⭐⭐ (4/5)               | ⭐⭐⭐⭐⭐ (5/5)                     | Hypothesis tests assert "A wins ties" as a universal invariant |
| **Exploration Rate**       | ⭐⭐⭐⭐ (4/5)    | ⭐⭐⭐⭐⭐ (5/5)              | ⭐⭐⭐ (3/5)                       | Conservative bias may reject novel-but-equal alternatives |
| **Convergence Speed**      | ⭐⭐⭐ (3/5)     | ⭐ (1/5)                  | ⭐⭐⭐⭐⭐ (5/5)                     | Ties extend streak in Option C; reset streak in B; random in A |

## Decision Outcome

**Chosen Option:** Option C: Always Incumbent Wins -- Conservative Tiebreak

**Rationale:**
The conservative tiebreak is the natural default for a system whose primary goal is quality improvement. The autoreason algorithm's convergence loop is designed around the principle that the incumbent represents the current best-known output. A challenger must **prove** it is better through a clear Borda score advantage, not merely match the incumbent. This aligns with the medical principle of "first, do no harm" -- if judges are split, the safe action is to keep what works.

The cost efficiency argument is equally compelling: with default settings (3 judges, convergence_k=2), a three-way tie in two consecutive passes means the tournament converges in 2 passes (2 A wins from tiebreak). Under Option B, the same ties would produce 2 non-A wins, resetting the streak and extending the tournament toward max_rounds -- potentially tripling LLM cost for no quality benefit.

The configurability via `TournamentConfig.conservative_tiebreak` means advanced users can opt into `tiebreak_winner=None` (label-order tiebreak) for experimentation, but the default is the safe choice.

**Key Factors:**
- Quality monotonicity: the output can only improve or stay the same, never regress, when conservative tiebreak is active
- The implementation is exactly 5 lines in `aggregate_rankings()`: a priority dict, a sorted call with a secondary key, and a single conditional
- Hypothesis property tests (`test_winner_has_max_score_or_wins_tiebreak`) prove the invariant holds for all possible ranking combinations with up to 10 judges

## Consequences

### Positive Consequences
- The tournament is a safe operation: running it on a good plan or implementation can never make it worse
- Cost is predictable: worst case is `max_rounds` passes (ties at every round still converge at `convergence_k` consecutive ties)
- Test assertions are simple: "if A is among the tied labels, winner must be A" is a universal invariant
- The degenerate case (all judges fail to parse) is handled gracefully: A wins with scores {A: 0, B: 0, AB: 0}, preserving the incumbent

### Negative Consequences / Trade-offs
- A genuinely-equal-quality alternative is always rejected in favor of the incumbent; this introduces a slight bias toward the initial proposal's structure and style
- If the initial input to the tournament is low quality, it takes a decisive majority of judges to displace it; the conservative tiebreak does not help poor initial conditions converge faster
- Users who want aggressive exploration must explicitly set `conservative_tiebreak: False` in their tournament config

### Neutral / Unknown Consequences
- The interaction between conservative tiebreak and convergence_k is multiplicative: higher k + conservative tiebreak = very stable convergence but potentially slower improvement; this trade-off should be documented in user-facing configuration guidance
- With an even number of judges (e.g., 2), ties become more common, making the tiebreak policy more consequential; the default `num_judges=3` (odd) mitigates this somewhat

## Implementation Notes

**Files Affected:**
- `src/tournament/core.py:76-84` - `TournamentConfig` dataclass with `conservative_tiebreak: bool = True` default
- `src/tournament/core.py:140-166` - `aggregate_rankings()` function implementing Borda aggregation with configurable tiebreak via `tiebreak_winner` parameter
- `src/tournament/core.py:290-293` - `Tournament.run_pass()` passes `tiebreak="A"` if `self.cfg.conservative_tiebreak` else `None` to `aggregate_rankings()`
- `tests/test_tournament_borda_aggregation.py` - 12 test cases + 3 Hypothesis property tests covering tiebreak behavior

**Ledger/State Implications:**
- Each pass result is recorded in `PassResult` with `winner`, `scores`, and `valid_judges` fields; the tiebreak policy is not recorded in the pass result itself but is a property of the tournament config
- Tournament completion is recorded as a `plan_tournament_complete` or `impl_tournament_complete` ledger operation
- `TournamentEvidence.winner` records the final tournament winner as `Literal["A", "B", "AB"]`; when the final pass was a tie, this will be "A" under conservative tiebreak
- None: no schema changes are required for this policy; it is purely a behavioral configuration

**General Guidance:**
- The `tiebreak_winner` parameter in `aggregate_rankings()` accepts any label string or `None`; setting it to `None` falls back to label-order tiebreak (first label in `labels` list wins ties)
- When adding a new tournament variant (e.g., 5-way judge rankings), the same tiebreak mechanism works: `tiebreak_winner` is checked against the `labels` list
- Property tests should always include the tiebreak invariant: "if `tiebreak_winner` is among the tied labels, it must be the winner"

## Evidence from Codebase

**Source References:**
- `src/tournament/core.py:83` - `conservative_tiebreak: bool = True` in `TournamentConfig`
- `src/tournament/core.py:140-166` - `aggregate_rankings()`: Borda scoring loop, tiebreak priority dict, sorted ranking with `(-scores[k], priority[k])` key
- `src/tournament/core.py:158-162` - Priority assignment: `priority = {label: (0 if label == tiebreak_winner else i + 1) for i, label in enumerate(labels)}`
- `src/tournament/core.py:291` - Pass-level wiring: `tiebreak = "A" if self.cfg.conservative_tiebreak else None`
- `src/tournament/core.py:292-293` - `winner, scores, valid_judges = aggregate_rankings(rankings, labels=["A", "B", "AB"], tiebreak_winner=tiebreak)`

**Test Coverage:**
- `tests/test_tournament_borda_aggregation.py::test_tiebreak_A_beats_B_on_equal_scores` - Three-way tie (4-4-4) with `tiebreak_winner="A"` asserts A wins
- `tests/test_tournament_borda_aggregation.py::test_tiebreak_AB_wins_over_A` - Same tie with `tiebreak_winner="AB"` asserts AB wins (confirms configurability)
- `tests/test_tournament_borda_aggregation.py::test_no_tiebreak_uses_label_order` - With `tiebreak_winner=None`, first label in list wins ties
- `tests/test_tournament_borda_aggregation.py::test_unequal_scores_beat_tiebreak` - B wins decisively (score 6 vs 3); tiebreak to A does NOT override, confirming tiebreak only applies to ties
- `tests/test_tournament_borda_aggregation.py::test_all_none_rankings_fallback_to_tiebreak` - All 3 judges return `None`; scores are {0,0,0}; A wins by tiebreak (degenerate case)
- `tests/test_tournament_borda_aggregation.py::test_empty_rankings_fallback_to_tiebreak` - Empty ranking list; A wins by tiebreak
- `tests/test_tournament_core.py::test_convergence_all_A_in_two_passes` - End-to-end tournament: judges always favor A, converges in 2 passes (streak=2 at k=2)

**Property-Based Tests (Hypothesis):**
- `tests/test_tournament_borda_aggregation.py::test_score_sum_invariant` - For any combination of up to 10 judges (with random `None` entries), total Borda points equal `valid * n * (n+1) / 2` (200 examples)
- `tests/test_tournament_borda_aggregation.py::test_winner_has_max_score_or_wins_tiebreak` - For any combination of judges: winner's score is the maximum; if A is among the tied labels, A must win (200 examples)
- `tests/test_tournament_borda_aggregation.py::test_valid_count_matches_non_none` - Valid judge count equals the number of non-None rankings (200 examples)

## Related Design Documents

- [tournaments.md](../../docs/design_documentation/tournaments.md) - Describes the autoreason self-refinement algorithm, convergence loop, and the role of conservative tiebreak in the convergence criteria
- [cost.md](../../docs/design_documentation/cost.md) - Tiebreak policy directly affects cost: conservative tiebreak accelerates convergence, reducing the number of passes (and LLM calls) per tournament
- [agents.md](../../docs/design_documentation/agents.md) - The `judge` agent produces the rankings that feed into Borda aggregation; parse failures (None rankings) are handled gracefully by the tiebreak policy

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: Tournament convergence in <= 50% of `max_rounds` on average; zero incidents where conservative tiebreak prevented a clearly-better alternative from winning (would require decisive score, not a tie)
- [ ] Metrics to track: Average convergence pass number, frequency of tiebreak-decided passes (passes where the winner had the same score as another candidate), judge parse failure rate (impacts effective vote count and tiebreak frequency)

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
