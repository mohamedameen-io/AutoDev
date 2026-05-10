# AutoDev Seed Packs

This directory holds **bootstrap knowledge packs** that are loaded into the
`hive` tier of the two-tier knowledge store
(`src/state/knowledge.py`) on the first orchestrator run of any project.

A seed pack gives reviewers / critics a baseline of well-known anti-patterns
to consult before any organic lessons have accumulated — without it, a brand
new project starts with an empty hive and gets no anti-bloat guidance until
several runs have produced enough confirmations to promote a swarm lesson.

## File layout

```
seeds/
  README.md              # this file
  anti_bloat_v1.jsonl    # 18 anti-bloat lessons (Karpathy, Austin, bloatware-detector, PyExamine)
  ...                    # future packs land here as <name>.jsonl
```

Each `.jsonl` file contains one JSON object per line, schema-compatible with
`state.knowledge.KnowledgeEntry`:

| field | value for seeds |
|---|---|
| `id` | `"ab_v1_001"` ... deterministic short id |
| `timestamp` | `"2026-05-09T00:00:00Z"` (literal) |
| `role_source` | `"seed_pack:<pack_name>"` |
| `tier` | `"hive"` |
| `text` | one anti-pattern lesson, ≤ ~250 chars |
| `confidence` | `0.85` |
| `metadata.lane` | `"anti_bloat"` (lane label for filtering) |
| `metadata.source_pack` | the pack name |
| `metadata.rule_source` | upstream rule family (`karpathy`, `austin`, `bloatware-detector`, `pyexamine`) |
| `metadata.smell_name` | one of the closed vocabulary smells (see below) |

### Closed smell vocabulary

Every entry's `metadata.smell_name` MUST be one of:

`long_method`, `duplicate_code`, `dead_code`, `feature_envy`,
`speculative_generality`, `shotgun_surgery`, `primitive_obsession`,
`complex_conditional`, `large_class`.

`tests/test_seed_pack_smell_names.py` enforces this.

## How loading works

`src/state/seed_packs.py` provides `seed_pack_if_missing()`. The orchestrator
calls it once per configured pack at the entry of each high-level operation
(`plan` / `execute` / `resume`). Loading is idempotent via two mechanisms:

1. **Marker file** — `<cwd>/.autodev/seed_packs.json` records which packs have
   been seeded for this project. A pack listed in the marker is skipped on
   subsequent runs (cheap short-circuit, avoids rereading the JSONL).
2. **Bigram-Jaccard dedup** — even without the marker, the underlying hive
   write path applies the same `dedup_threshold` (default 0.6) used elsewhere
   in the knowledge store, so re-loading a pack cannot produce duplicates.

## Operator controls

* **Disable seeding entirely**: set `knowledge.seed_packs_enabled = false`
  in `.autodev/config.json`.
* **Restrict which packs load**: set `knowledge.seed_packs = []` (or a
  filtered list) in `.autodev/config.json`.
* **Reject a noisy seeded entry**: use the existing rejection path
  (`KnowledgeStore.reject(lesson_id, reason)`). Rejected entries are
  recorded in `.autodev/rejected_lessons.jsonl` and blocked from re-learning
  via Jaccard match — including against future seed re-loads.
* **Re-seed after schema change**: delete `.autodev/seed_packs.json` (and
  optionally clear matching entries from the hive) and re-run.

## Adding a new pack

1. Create `seeds/<pack_name>.jsonl` with the schema above.
2. Add `"<pack_name>"` to `KnowledgeConfig.seed_packs` defaults in
   `src/config/schema.py` (and to the relevant operator's `.autodev/config.json`
   if older configs already exist on disk).
3. Add a smell-vocabulary test (or extend the existing one) so the pack is
   covered by the same closed-vocabulary contract.
