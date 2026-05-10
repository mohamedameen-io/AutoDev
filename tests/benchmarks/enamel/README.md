# enamel

Cache directory for the **ENAMEL** benchmark — efficient code generation
evaluation (eff@k metric, expert-written reference solutions). Used by
the longitudinal anti-bloat harness (Phase 6) to verify that lean code
remains performant, not just short.

**Source:** https://github.com/q-rz/enamel

**Populate locally:**

```bash
git clone https://github.com/q-rz/enamel.git \
  tests/benchmarks/enamel/_repo
```

The `.gitkeep` placeholder reserves the directory; downloaded data is
gitignored. Phase 0 only stubs the location — actual download is deferred
to Phase 6.
