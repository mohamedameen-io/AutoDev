# yapbench

Cache directory for the **yapbench** dataset — a benchmark for measuring
LLM verbosity ("yapping") on code-generation tasks. Used by the longitudinal
anti-bloat harness (Phase 6) to evaluate whether AutoDev's leanness
interventions reduce model output bloat compared to baseline.

**Source:** https://huggingface.co/datasets/tabularisai/yapbench_dataset

**Populate locally:**

```bash
huggingface-cli download tabularisai/yapbench_dataset \
  --repo-type dataset \
  --local-dir tests/benchmarks/yapbench
```

The `.gitkeep` placeholder reserves the directory; downloaded data is
gitignored. Phase 0 only stubs the location — actual download is deferred
to Phase 6.
