# ADR-004: Two-Tier Knowledge Store with Jaccard Deduplication

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** knowledge, deduplication, jaccard, two-tier, swarm, hive, ranking, offline-first
**Related ADRs:** ADR-002 (knowledge uses the same JSONL + atomic-write patterns as the ledger)

## Context

AutoDev's agents learn from their work: successful patterns, common pitfalls, and project-specific conventions are captured as "lessons" that can be injected into future agent prompts. This knowledge system must handle two distinct scopes:

- **Swarm** (per-project): lessons specific to the current codebase, stored in `<cwd>/.autodev/knowledge.jsonl`
- **Hive** (global): cross-project wisdom promoted from mature swarm entries, stored in `~/.local/share/autodev/shared-learnings.jsonl`

The system must deduplicate incoming lessons against existing ones (agents often re-learn the same insight), rank lessons for injection into prompts (limited context window), and operate entirely offline with zero external service dependencies. It must also support a rejection mechanism (operator removes a bad lesson permanently) and a promotion pathway (high-confidence, frequently-confirmed swarm lessons graduate to the hive).

The deduplication algorithm is the key technical decision: it determines whether two lesson texts are "the same" and should be merged rather than stored separately. False negatives (missed duplicates) waste context window space; false positives (over-aggressive dedup) destroy distinct lessons.

## Options Considered

### Option A: Single Flat File (No Dedup, No Tiers)

**Description:**
Store all lessons in a single `knowledge.jsonl` file. No deduplication -- every recorded lesson is appended. No global tier -- lessons are project-local only. Ranking is by recency (newest first). Cap the file at a maximum entry count; evict oldest entries when full.

**Pros:**
- Simplest possible implementation: append and read
- No false positives from dedup (every lesson is preserved as recorded)
- No cross-project concerns or global state
- Trivial to reason about: newer = better

**Cons:**
- Rapid duplication: agents re-learn the same insight across sessions, filling the cap with identical lessons
- No cross-project knowledge transfer: insights from project A are invisible to project B
- Recency-only ranking ignores confidence and usage, producing poor injection quality
- No rejection mechanism: a bad lesson persists until evicted by age
- Context window waste: injecting 10 copies of the same lesson displaces 9 unique ones

### Option B: Vector Database with Embeddings

**Description:**
Store lessons as embedding vectors in a local vector database (e.g., ChromaDB, FAISS). Deduplication uses cosine similarity between embedding vectors. Semantic search retrieves the most relevant lessons for a given prompt context. Two-tier support via separate collections.

**Pros:**
- Semantic deduplication: "use async locks" and "prefer asyncio-based locking" are recognized as near-duplicates
- Context-aware retrieval: inject lessons most relevant to the current task, not just highest-ranked globally
- Rich query capabilities (k-nearest-neighbor search)
- Industry-standard approach for RAG systems

**Cons:**
- Requires an embedding model to generate vectors, adding an external dependency (local model or API call)
- Embedding quality varies across models; requires evaluation and possible re-embedding when switching models
- Binary database files are not human-readable or debuggable with standard tools
- Offline operation requires bundling or pre-downloading an embedding model
- Non-deterministic: different embedding models produce different similarity scores for the same inputs
- Adds significant dependency weight (ChromaDB: ~50MB; sentence-transformers: ~400MB+)
- Similarity threshold tuning is empirical and model-dependent

### Option C: Two-Tier JSONL with Bigram Jaccard Dedup

**Description:**
Lessons are stored in two JSONL files (swarm per-project, hive global). Deduplication uses character-level bigram Jaccard similarity: for two texts, compute the set of consecutive character pairs (bigrams), then measure `|intersection| / |union|`. Texts with similarity >= 0.6 (configurable threshold) are treated as duplicates and merged (confidence boosted, confirmation count incremented). Ranking combines `confidence * recency_factor * (1 + log(applied_count + 1))`. Promotion from swarm to hive requires minimum confirmations and minimum confidence. A rejection list blocks re-learning of removed lessons via the same Jaccard similarity check.

**Pros:**
- Zero external dependencies: uses only stdlib string operations and set arithmetic
- Deterministic: same inputs always produce the same similarity score (no model variance)
- Human-explainable: "these two texts share 65% of their character bigrams" is intuitive to debug
- Offline-first: no network, no embedding model, no database engine
- JSONL format is human-readable, greppable, and git-friendly
- Two-tier architecture enables both project-specific and cross-project knowledge
- Rejection list prevents re-learning of operator-rejected lessons
- Configurable threshold (default 0.6) allows tuning precision/recall trade-off
- Case-insensitive: lowercases before computing bigrams
- Symmetric: `jaccard(a, b) == jaccard(b, a)` always holds
- Atomic writes via `tmp + os.replace` match the ledger's crash-safety pattern

**Cons:**
- Character bigrams are syntactic, not semantic: "use locks" and "employ synchronization primitives" have low similarity despite meaning the same thing
- The 0.6 threshold is empirically chosen; may need tuning for different text lengths or domains
- Jaccard similarity on short texts (< 10 characters) is unreliable (few bigrams create high variance)
- No context-aware retrieval: ranking is global, not task-specific
- Linear scan for dedup: O(n * m) where n = existing entries and m = bigram comparison cost. Acceptable for hundreds of entries but would need indexing for thousands
- Recency decay is time-based (30-day linear decay to 0.5 floor), which may not reflect actual lesson relevance

## Decision Drivers

- **Zero External Dependencies:** Must not require any package beyond what AutoDev already bundles
- **Offline-First:** Must work without network access, embedding APIs, or database servers
- **Deterministic Dedup:** Same texts must always produce the same similarity score
- **Explainable Ranking:** An operator must be able to understand why lesson X ranks above lesson Y
- **Crash Safety:** Atomic writes; no corrupted files on SIGKILL
- **Debuggability:** Lessons must be inspectable with `cat`/`jq` without specialized tooling
- **Pydantic-Boundary Strictness:** Lesson entries validated via Pydantic models

## Architecture Drivers Comparison

| Architecture Driver           | Option A: Flat File | Option B: Vector DB | Option C: Jaccard + Two-Tier | Notes |
|-------------------------------|---------------------|---------------------|-----------------------------|-------|
| **Zero Dependencies**         | ⭐⭐⭐⭐⭐              | ⭐                  | ⭐⭐⭐⭐⭐                      | A: stdlib only; B: requires embedding model + DB; C: stdlib + filelock (already a dependency) |
| **Offline-First**             | ⭐⭐⭐⭐⭐              | ⭐⭐                 | ⭐⭐⭐⭐⭐                      | B requires embedding model; A and C are fully offline |
| **Deterministic Dedup**       | N/A                 | ⭐⭐                 | ⭐⭐⭐⭐⭐                      | A: no dedup; B: model-dependent; C: pure set arithmetic |
| **Explainable Ranking**       | ⭐⭐                  | ⭐⭐                 | ⭐⭐⭐⭐⭐                      | A: recency only; B: opaque embeddings; C: `conf * recency * log_boost` formula |
| **Crash Safety**              | ⭐⭐⭐                 | ⭐⭐⭐⭐              | ⭐⭐⭐⭐⭐                      | A: simple append; B: DB WAL; C: atomic tmp+replace |
| **Debuggability**             | ⭐⭐⭐⭐⭐              | ⭐                  | ⭐⭐⭐⭐⭐                      | A: plain JSONL; B: binary; C: plain JSONL with readable fields |
| **Dedup Quality**             | ⭐                   | ⭐⭐⭐⭐⭐             | ⭐⭐⭐                         | B: semantic similarity; C: syntactic (misses paraphrases); A: no dedup |
| **Cross-Project Knowledge**   | ⭐                   | ⭐⭐⭐⭐              | ⭐⭐⭐⭐                        | A: none; B: separate collections; C: swarm -> hive promotion |
| **Scalability**               | ⭐⭐                  | ⭐⭐⭐⭐⭐             | ⭐⭐⭐                         | B: indexed retrieval; C: linear scan; A: linear but no dedup overhead |

## Decision Outcome

**Chosen Option:** Option C: Two-Tier JSONL with Bigram Jaccard Dedup

**Rationale:**
The zero-dependency and offline-first requirements are non-negotiable for AutoDev, which targets developers who may be working on air-gapped machines, in transit, or without API credits. Option B's embedding approach is superior for dedup quality but violates both requirements. Option A is simpler but the lack of deduplication causes rapid knowledge bloat that degrades injection quality.

Option C provides "good enough" dedup quality for the actual distribution of lesson texts in practice. Lessons generated by LLM agents tend to use similar phrasing when describing the same concept, which character-bigram Jaccard handles well. The threshold of 0.6 was empirically validated: `test_threshold_range` confirms that highly similar pairs clear the threshold while genuinely different texts fall below it.

**Key Factors:**
- The ranking formula `confidence * recency_factor * (1 + log(applied_count + 1))` combines three orthogonal signals: how reliable the lesson is (confidence), how fresh it is (recency), and how often it has been successfully applied (applied_count). Each factor is independently testable.
- The promotion mechanism (swarm -> hive) requires both minimum confirmations and minimum confidence, preventing low-quality lessons from polluting the global store. Promotion is idempotent: near-duplicate detection in the hive prevents double-promotion.
- The rejection list uses the same Jaccard similarity to block re-learning, ensuring that once an operator removes a lesson, agents cannot re-record a trivially rephrased version.
- The `_denylist()` mechanism prevents knowledge injection into stateless/fact-finding roles (e.g., critics, judges) that should not be biased by prior lessons.

## Consequences

### Positive Consequences
- AutoDev works identically on air-gapped machines, CI runners, and developer laptops. No embedding model download, no API key, no database server.
- Knowledge files are human-readable and version-controllable. A team can `cat .autodev/knowledge.jsonl | jq .` to inspect lessons, or `git diff` to see what lessons changed between commits.
- The dedup merge operation (confidence boost + confirmation increment) creates a natural signal: lessons that are independently re-learned across sessions accumulate higher confidence and eventually promote to the hive.
- Capacity caps (`swarm_max_entries`, `hive_max_entries`) with lowest-ranked eviction ensure the knowledge store never grows unbounded, and the evicted entries are the least useful.
- The `inject_block()` method produces a compact text block with confidence annotations (`[conf:0.80]`) that agents can use to calibrate their reliance on each lesson.

### Negative Consequences / Trade-offs
- Syntactic dedup misses semantic equivalences: "prefer async locks" and "use non-blocking synchronization" will not be recognized as duplicates. This leads to some wasted capacity but does not cause incorrect behavior.
- The 0.6 Jaccard threshold is a single global value. Very short lessons (< 20 characters) may produce unreliable similarity scores due to few bigrams. The `_truncate()` function mitigates the other extreme (very long lessons).
- Linear scan for dedup is O(n) per record call. With the default `swarm_max_entries` cap, this is fast. If the cap were raised to thousands, an index structure would be needed.
- Cross-tier dedup during injection (`inject_block`) compares every hive entry against every swarm entry in the merged list, which is O(swarm * hive). Acceptable for the default caps but would need optimization at scale.

### Neutral / Unknown Consequences
- The recency decay window (30 days, linear to 0.5 floor) may need tuning for long-running projects where lessons from months ago are still relevant. The floor of 0.5 ensures old lessons are never fully discounted.
- If AutoDev adds semantic search in the future (e.g., for task-specific lesson retrieval), it could be layered on top of the JSONL store as a read-time index, without changing the write path.

## Implementation Notes

**Files Affected:**
- `src/state/knowledge.py` - `KnowledgeStore` class, `KnowledgeEntry` model, `RejectedLesson` model, `jaccard_bigrams()`, `_bigrams()`, `_recency_factor()`, `_atomic_write()`, `_read_jsonl()`, `_write_jsonl()`
- `src/config/schema.py` - `KnowledgeConfig` with `dedup_threshold`, `swarm_max_entries`, `hive_max_entries`, `promotion_min_confirmations`, `promotion_min_confidence`, `denylist_roles`, `max_inject_count`
- `src/state/paths.py` - `knowledge_path()` and `rejected_lessons_path()` for per-project files
- `src/state/lockfile.py` - `plan_lock()` used for swarm writes; `filelock.FileLock` used for hive writes

**Ledger/State Implications:**
- Knowledge state is separate from the plan ledger (ADR-002). Knowledge JSONL files do not participate in the hash chain.
- No new `LedgerOp` types are needed; knowledge operations are recorded in their own files.

**General Guidance:**
- Always use `jaccard_bigrams()` for similarity checks, never raw string equality. The function handles case-insensitivity and empty-string edge cases.
- All JSONL writes must use `_atomic_write()` (tmp + os.replace) for crash safety.
- When adding new fields to `KnowledgeEntry`, ensure backward compatibility: existing JSONL files may not have the new field. Pydantic defaults handle this.
- Swarm writes hold `plan_lock`; hive writes hold the hive-specific `FileLock`. Never hold both simultaneously to avoid deadlocks.
- The `_MAX_LINE_BYTES` constant (64 KB) prevents a single oversized lesson from bloating the JSONL file. Lessons exceeding this are truncated with a warning.

## Evidence from Codebase

**Source References:**
- `src/state/knowledge.py:138-157` - `jaccard_bigrams()`: computes character-level bigram sets, returns `|intersection| / |union|`; handles empty and single-char edge cases
- `src/state/knowledge.py:86-98` - `KnowledgeEntry` model: id, timestamp, role_source, tier, text, confidence, applied_count, confirmations, metadata
- `src/state/knowledge.py:331-453` - `record()`: rejection guard -> swarm dedup -> merge or fresh entry -> capacity eviction -> promotion check
- `src/state/knowledge.py:592-599` - `_rank_with_ts()`: `confidence * recency_factor * (1 + log(applied_count + 1))`
- `src/state/knowledge.py:127-135` - `_recency_factor()`: linear decay from 1.0 (now) to 0.5 (30 days), floor at 0.5
- `src/state/knowledge.py:517-588` - `inject_block()`: denylist check -> rank swarm -> rank hive -> swarm-first merge with cross-tier Jaccard dedup -> format with confidence annotations
- `src/state/knowledge.py:612-659` - `_promote_if_qualified()`: checks hive_enabled, min_confirmations, min_confidence, hive Jaccard dedup, then atomically appends under hive lock
- `src/state/knowledge.py:211-214` - `_atomic_write()`: tmp + os.replace pattern matching the ledger's crash-safety approach

**Test Coverage:**
- `tests/test_knowledge_jaccard.py::test_identical_strings_are_one` - Verifies jaccard(x, x) == 1.0
- `tests/test_knowledge_jaccard.py::test_disjoint_strings_are_zero` - Verifies completely different alphabets produce 0.0
- `tests/test_knowledge_jaccard.py::test_empty_strings_return_zero` - Both empty -> 0.0 (incomparable, not identical)
- `tests/test_knowledge_jaccard.py::test_case_insensitive` - "Hello" vs "HELLO" matches "hello" vs "hello"
- `tests/test_knowledge_jaccard.py::test_symmetric` - jaccard(a, b) == jaccard(b, a)
- `tests/test_knowledge_jaccard.py::test_threshold_range` - Highly similar pair clears the 0.6 threshold
- `tests/test_knowledge_ranking.py::test_higher_confidence_ranks_higher` - Confidence 0.9 outranks 0.2
- `tests/test_knowledge_ranking.py::test_more_recent_ranks_higher` - Fresh entry outranks 20-day-old entry
- `tests/test_knowledge_ranking.py::test_stale_saturates_at_recency_floor` - 60-day-old entry has recency factor 0.5
- `tests/test_knowledge_ranking.py::test_higher_applied_count_ranks_higher` - applied_count=10 outranks applied_count=0
- `tests/test_knowledge_ranking.py::test_zero_applied_count_still_ranks` - applied_count=0 gives multiplier 1.0, not 0
- `tests/test_knowledge_promotion.py::test_promotes_after_three_confirmations_high_confidence` - 3 confirmations + 0.8 confidence -> promoted to hive
- `tests/test_knowledge_promotion.py::test_two_confirmations_is_not_enough` - 2 confirmations -> no promotion
- `tests/test_knowledge_promotion.py::test_low_confidence_is_not_promoted` - Even 5 confirmations with low confidence -> no promotion
- `tests/test_knowledge_promotion.py::test_promotion_is_idempotent` - 6 confirmations -> exactly 1 hive entry (not 4)

**Property-Based Tests (Hypothesis):**
- N/A for knowledge tests (scenario-based with specific similarity pairs and ranking formula verification)

## Related Design Documents

- [adapters.md](../../docs/design_documentation/adapters.md) - Knowledge injection happens in the prompt composition step before each adapter call
- [agents.md](../../docs/design_documentation/agents.md) - Agent role definitions determine which roles are on the denylist for knowledge injection
- [cost.md](../../docs/design_documentation/cost.md) - Knowledge injection adds tokens to prompts; max_inject_count controls the budget
- [architecture.md](../../docs/architecture.md) - Knowledge store's position as a cross-cutting concern across the orchestrator

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: Dedup catches >= 80% of semantically equivalent re-learnings; injection quality improves agent performance (measured by tournament convergence speed); no false-positive dedup reports from operators
- [ ] Metrics to track: Dedup merge rate (how often `record()` merges vs creates new); promotion rate (swarm -> hive); rejection list size; average Jaccard score at dedup boundary (entries near 0.6 threshold); inject_block token count distribution

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
