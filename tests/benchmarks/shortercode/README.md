# shortercode

Cache directory for the **ShorterCode** dataset — paired (verbose, shorter)
code samples used to evaluate code-shortening / minimality scoring. Feeds
the longitudinal anti-bloat harness (Phase 6) and minimality_judge
calibration (Phase 7).

**Source:** https://github.com/DeepSoftwareAnalytics/ShorterCode

**Populate locally:**

```bash
git clone https://github.com/DeepSoftwareAnalytics/ShorterCode.git \
  tests/benchmarks/shortercode/_repo
```

The `.gitkeep` placeholder reserves the directory; downloaded data is
gitignored. Phase 0 only stubs the location — actual download is deferred
to Phase 6.
