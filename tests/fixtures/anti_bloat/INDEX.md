# Anti-Bloat Fixture Index

Paired examples of LLM-shaped bloat vs minimal idiomatic Python. Each pair
preserves functional behavior — pytest of the public API would pass on both.

## Provenance note (v1 bootstrap)

All 10 pairs are **synthetic** for the v1 bootstrap. The plan permits ≤30%
synthetic in steady state, but the initial harness needs a starting corpus
before we can mine real AutoDev runs. Pairs were hand-crafted to mimic the
exact shape of code Claude/GPT/Gemini emit when nudged toward "extensible"
or "robust" designs (speculative ABCs, defensive None checks, comment
restatements, etc).

Future revisions should replace synthetic pairs with mined real-run pairs
once the longitudinal harness (Phase 6) has produced enough material.

## Smell vocabulary

Drawn from PyExamine (Bohr et al.) + standard refactoring catalog. Closed set:
`long_method`, `duplicate_code`, `dead_code`, `feature_envy`,
`speculative_generality`, `shotgun_surgery`, `primitive_obsession`,
`complex_conditional`, `large_class`.

## Manifest

| pair_id | smell_name | source | rule_source citation |
|---------|------------|--------|----------------------|
| pair_01_speculative_abstraction | speculative_generality | synthetic | Fowler refactoring catalog "Speculative Generality"; Karpathy "premature abstraction is the root of all evil" |
| pair_02_defensive_scaffolding | dead_code | synthetic | Austin "if the type system already proves it, the check is dead"; PyExamine dead-code detector |
| pair_03_restated_comments | dead_code | synthetic | Karpathy "comments that restate code are noise"; PEP 257 (docstrings should add intent, not restate) |
| pair_04_unused_config_knob | dead_code | synthetic | PyExamine unused-parameter rule; Bohr "config knobs that no caller exercises are dead surface" |
| pair_05_one_call_helper | speculative_generality | synthetic | Fowler "Inline Function" refactor; Karpathy "don't extract until you have a second caller" |
| pair_06_redundant_try_except | dead_code | synthetic | Austin "exception handlers that re-raise add noise without value"; PEP 8 "be specific in except clauses" |
| pair_07_dead_import | dead_code | synthetic | PyExamine unused-import rule; pyflakes F401 |
| pair_08_duplicate_logic | duplicate_code | synthetic | Fowler "Extract Function"; PyExamine duplicate-block detector; DRY (Hunt & Thomas) |
| pair_09_feature_envy | feature_envy | synthetic | Fowler refactoring catalog "Feature Envy"; PyExamine attribute-chain detector |
| pair_10_premature_dataclass | speculative_generality | synthetic | Karpathy "single-field structs are usually a smell"; Fowler "Inline Class" |

## File layout

For each pair `NN_<name>`:
- `pair_NN_<name>.py` — verbose / bloat-shaped version
- `pair_NN_<name>.lean.py` — minimal idiomatic version

See `metrics_baseline.json` for hand-counted before/after metric deltas.
These are placeholders for v1; Phase 1 will regenerate with `radon` +
`vulture` + the real static-analysis pipeline.
