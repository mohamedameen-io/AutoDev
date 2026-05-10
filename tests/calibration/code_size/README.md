# Code-size calibration corpus

50-PR calibration corpus used to promote individual `code_size` rules from
`severity="warn"` to `severity="block"`. Empty in v1; populate as PRs
accumulate against AutoDev's history.

## Promotion criterion

Per the plan (§Phase 1, "Calibration-first promotion policy"):

> A rule may be promoted from `warn` to `block` only after measured
> precision >= 85% on a 50-PR calibration sample.

The 85% precision floor is anchored to two empirical numbers:

* **PyExamine §IV-B**: 87.23% inter-evaluator agreement among human
  reviewers — a realistic ceiling for any rule.
* **Cordeiro §II-B (Liu et al.)**: 7.4% LLM-refactoring failure rate —
  a defensible false-positive budget.

## Corpus shape

Each calibration entry is one PR with two files in this directory:

```
tests/calibration/code_size/
  PR-<number>/
    diff.patch          # the unified diff to scan
    label.json          # human label: bloat / not-bloat per rule
```

`label.json` schema:

```json
{
  "pr_number": 123,
  "repo": "mohamedameen/autodev",
  "labels": {
    "cyclomatic_max": "bloat",
    "long_functions": "not-bloat",
    "dead_symbols": "bloat",
    "commented_out_blocks": "not-bloat",
    "duplicate_clusters": "not-bloat"
  },
  "labeller": "human",
  "labelled_at": "2026-XX-XXT00:00:00Z"
}
```

## Running the calibration

```
uv run python scripts/calibrate_code_size.py
```

Per-rule precision is reported. The script exits 0 on an empty corpus so it
doesn't break CI on day one.
