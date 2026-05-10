# AutoDev: References for Anti-Bloat / Code-Size Optimization

## Purpose

AutoDev currently optimizes for correctness, architectural fit, and crash-safety. It does not have a first-class mechanism for detecting or reducing the kind of bloat that single-agent LLM coding tools tend to introduce: speculative abstractions, over-commenting, dead helpers, defensive scaffolding that the type system already rules out, configuration knobs with one call site, and try/except blocks that don't materially handle anything.

This document collects sources that inform a possible anti-bloat capability for AutoDev. It is organized by category, not by feature. Each entry notes what the source provides and where in AutoDev's architecture (QA gates, tournament judge, reviewer/critic prompts, knowledge ledger, post-merge tooling) it could plausibly be drawn upon.

---

## 1. Static analysis tools (deterministic signals)

These are the building blocks for a bloat-detection QA gate. None of them require an LLM call; all produce structured output suitable for evidence bundles.

- **Radon** — https://github.com/rubik/radon, https://radon.readthedocs.io/
  Cyclomatic complexity, Halstead metrics, maintainability index, raw LOC counts. AST-based, programmatic API. Gives per-function complexity ranks (A–F).

- **Vulture** — https://github.com/jendrikseipp/vulture
  Dead code detection: unused imports, classes, functions, variables, attributes, unreachable code. Confidence-rated findings.

- **mccabe** — https://github.com/PyCQA/mccabe
  Cyclomatic complexity checker. Lighter than Radon, used internally by Flake8.

- **Ruff** — https://github.com/astral-sh/ruff, https://docs.astral.sh/ruff/rules/
  Has rules for the patterns LLMs over-produce: too-many-statements (PLR0915), too-many-branches (PLR0912), unused-argument (ARG), unnecessary-pass, broad-except, redundant-bool-comparison. Useful as a pre-built rule set; pieces of it can be turned on selectively for diff-only checks.

- **PyExamine** — https://arxiv.org/abs/2501.18327
  Recent (2025) academic Python smell detector. Goes beyond Pylint/Radon to detect architectural smells (cyclic dependencies, god components, scattered functionality, problematic inheritance). Useful for the patterns that creep in across multiple LLM-generated tasks.

- **eradicate** — https://github.com/wemake-services/eradicate
  Detects commented-out code blocks. LLMs frequently leave these behind during refactor passes.

- **Lizard** — https://github.com/terryyin/lizard
  Multi-language complexity analyzer. Worth knowing about if AutoDev ever wants to support non-Python codebases.

- **The Wikipedia "Signs of AI Writing" guide** — referenced in the `/mnt/skills/user/humanizer/SKILL.md` shipped with this Claude environment.
  Originally for prose, but the patterns transfer: inflated framing, vague attribution, redundant parallelisms. Useful as a starting taxonomy when designing custom AST checks for natural-language elements (comments, docstrings, log messages) added by LLMs.

---

## 2. Research on LLM code brevity / size

This is a small but growing literature. None of these papers solves the AutoDev problem directly, but they establish the metrics, datasets, and taxonomies that a bloat capability should align with.

- **ShortCoder: Knowledge-Augmented Syntax Optimization for Token-Efficient Code Generation** — https://arxiv.org/abs/2601.09703 (arXiv 2601.09703)
  Releases **ShorterCodeBench**: 828 curated `(original_code, simplified_code)` pairs. Reports 18.1% generation efficiency improvement on HumanEvalPlus while preserving correctness. The dataset is the most directly usable artifact in this space — it can be used as a few-shot exemplar pool or as a reference for designing review-prompt criteria.

- **Token Sugar: Making Source Code Sweeter for LLMs through Token-Efficient Shorthand** — https://arxiv.org/abs/2512.08266
  Distinguishes "functional verbosity" (necessary syntax) from "non-functional verbosity" (readability scaffolding). The 799-pattern shorthand catalog enumerates the verbose patterns LLMs over-emit. Useful as an explicit list for AST checks or for the reviewer prompt's checklist.

- **AI Coders Are Among Us: Rethinking Programming Language Grammar Towards Efficient Code Generation** — https://arxiv.org/abs/2404.16333
  Articulates the gap between human-readability scaffolding and what models actually need. Useful background for distinguishing "comments that help maintainers" from "comments that restate the next line."

- **Show and Tell: Prompt Strategies for Style Control in Multi-Turn LLM Code Generation** — https://arxiv.org/abs/2511.13972
  Empirical comparison of prompt strategies for verbosity control across 160 two-turn sessions. Finding: directive-based prompts ("minimal code that works") preserve compression discipline across turns; example-based prompts inflate badly on follow-up turns. Directly relevant to designing developer/reviewer prompts.

- **YapBench: Do Chatbot LLMs Talk Too Much?** — https://arxiv.org/abs/2601.00624
  Not code-specific, but defines the **verbosity-compensation (VC) score** metric and documents that GPT-4 shows ~50% VC frequency on some tasks. The VC framework generalizes to code (extra abstractions, extra defensive checks) and is worth borrowing as a metric definition.

- **CodeRefine: A Pipeline for Enhancing LLM-Generated Code Implementations of Research Papers** — https://arxiv.org/abs/2408.13366
  Multi-step pipeline with retrospective retrieval-augmented generation. Less about bloat specifically, more about the structure of a refinement pipeline that could host a slimming pass.

- **Don't Transform the Code, Code the Transforms (Cummins et al., Meta)** — https://arxiv.org/abs/2410.08806
  Argues that LLMs should *generate transformation functions* rather than apply transformations directly. Inspectable, debuggable, far cheaper at scale. The most intellectually serious critique of direct-LLM-rewrite approaches and worth reading before committing to a slim-via-LLM-rewrite design.

---

## 3. LLM-as-judge with structured rubrics

AutoDev's tournament already uses Borda-aggregated judges. The literature on rubric-based judging gives concrete guidance for adding a minimality dimension without breaking the existing convergence properties.

- **G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment (Liu et al., EMNLP 2023)** — https://arxiv.org/abs/2303.16634
  Most-cited paper on rubric-based LLM judging. Generates evaluation chain-of-thought from task introduction and rubric, then scores. Pattern that decomposes well across multiple rubric dimensions.

- **Promptfoo LLM-as-Judge Guide** — https://www.promptfoo.dev/docs/guides/llm-as-a-judge/
  Practical patterns: `llm-rubric`, `g-eval`, `select-best`, multi-judge voting, injection-safe judge prompts. Has reference judge prompt templates that can be adapted for code-minimality scoring.

- **Langfuse LLM-as-a-Judge documentation** — https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge
  Covers numeric vs. categorical vs. boolean scoring tradeoffs and how to track judge scores over time. Relevant to the question of whether minimality should be a 1–5 score or a binary flag.

- **LLMs Cannot Reliably Judge (Yet?)** — https://arxiv.org/abs/2506.09443
  Robustness study of LLM-as-judge systems. Documents position bias, verbosity bias, and self-preference bias. Particularly relevant: judges have a documented bias toward longer answers (verbosity bias), which directly works against a minimality criterion. Mitigation strategies discussed here should inform any minimality rubric.

- **Monte Carlo: 7 Best Practices & Evaluation Templates** — https://www.montecarlodata.com/blog-llm-as-judge/
  Argues for criteria decomposition: each judge monitors a single criterion. Suggests minimality should be a separate axis from correctness rather than blended into one score.

- **Towards Data Science: LLM-as-a-Judge Practical Guide** — https://towardsdatascience.com/llm-as-a-judge-a-practical-guide/
  Distinguishes single-output scoring vs. comparison/ranking vs. binary labeling. Direct relevance to whether AutoDev should add minimality as a fourth Borda-rankable dimension or as a separate gate.

---

## 4. Anti-pattern catalogs from practitioners

The academic literature is thinner than the practitioner catalog on which patterns LLMs actually produce. These are the most concrete enumerations.

- **xtrasmal/bloatware-detector (gist)** — https://gist.github.com/xtrasmal/69aeacc002408010f475477a1c4187c5
  A working sub-agent prompt for Claude Code / Cursor, explicitly designed to flag "speculative implementations that add complexity without clear value — the kind of code an overzealous LLM added without explicit user request." Defines a structured `@bloatware [filename:line] Issue / Expected / Found / Action / Justification` output format. Drop-in reference for prompt design.

- **forrestchang/andrej-karpathy-skills** — https://github.com/forrestchang/andrej-karpathy-skills
  Karpathy's coding rules packaged as a Claude Code plugin. Core anti-bloat rules verbatim: "no features beyond what was asked, no abstractions for single-use code, no flexibility/configurability that wasn't requested, no error handling for impossible scenarios, if 200 lines could be 50 rewrite it." Plus the testing rule "every changed line should trace directly to the user's request."

- **"LLMs Have Revived These 5 Anti-Patterns in Software Engineering" (Derek Austin, Medium)** — https://medium.com/according-to-context/llms-have-revived-these-5-anti-patterns-in-software-engineering-e685159fc4d8
  Ranked enumeration of LLM code anti-patterns by frequency: over-commenting, excessive print statements, etc. Useful as a frequency-weighted priority list when deciding which AST checks to implement first.

- **"Era of AI Slop Cleanup Has Begun" (Ankur Tyagi, Bytesized Bets)** — https://bytesizedbets.com/p/era-of-ai-slop-cleanup-has-begun
  Industry perspective from freelance engineers cleaning up LLM-generated codebases. Less technical, useful for framing the problem and identifying which downstream symptoms (review burden, context-window pollution, security issues) the upstream bloat-detection should target.

- **"LLM-Driven Code Refactoring: Opportunities and Limitations" (Queen's, 2025)** — https://seal-queensu.github.io/publications/pdf/IDE-Jonathan-2025.pdf
  Survey of where LLM refactoring fails. Identifies the "how vs. why" prompt gap: prompts ask LLMs *how* to refactor without explaining *why*. Relevant to designing the prompt for a slimming pass — must include the reason for slimming, not just the instruction.

---

## 5. Existing LLM-aware code review and refactoring tools

Reference implementations for the agentic / pipeline pieces, even when their primary focus isn't bloat.

- **Nayjest/Gito** — https://github.com/Nayjest/Gito
  Vendor-agnostic AI code review tool. Stateless, zero-retention, runs in CI. Supports OpenAI-compatible APIs and local models (Ollama, vLLM, LM Studio). Reference for how to structure a CI-friendly review tool with structured output (JSON + Markdown reports).

- **codedog-ai/codedog** — https://github.com/codedog-ai/codedog
  Code review assistant with multi-dimensional scoring (correctness, readability, maintainability). Reference for scoring schema.

- **tusgino/llm-code-reviewer** — https://github.com/tusgino/llm-code-reviewer
  GitHub Action with multi-LLM support (OpenAI, Gemini, Anthropic). Reference for the GitHub-Action wrapper layer if AutoDev ever wants to expose a `gh-action` integration.

- **llm-refactoring/llm-refactoring-plugin** — https://github.com/llm-refactoring/llm-refactoring-plugin
  Academic IntelliJ plugin focused on Extract Method refactoring with peer-reviewed evaluation data (`tool_evaluation__extended_corpus_*.csv`). Datasets useful for benchmarking any AutoDev refactoring pass against an oracle.

---

## 6. Performance optimization research (adjacent, not the goal)

Listed for completeness because the field is dominated by speed-optimization rather than size-optimization, and AutoDev should not borrow uncritically from this work. The infrastructure (PIE benchmark, evaluation harness) is reusable; the optimization targets are not.

- **PIE: Learning Performance-Improving Code Edits** — https://arxiv.org/abs/2302.07867, https://pie4perf.com/
- **ECO: Enhanced Code Optimization via Performance-Aware Prompting** — https://arxiv.org/abs/2510.10517
- **PerfCoder** — https://arxiv.org/abs/2512.14018
- **FasterPy** — https://arxiv.org/abs/2512.22827, https://github.com/WuYue22/fasterpy
- **EffiBench-X: A Multi-Language Benchmark for Measuring Efficiency of LLM-Generated Code** — https://arxiv.org/abs/2505.13004
- **ENAMEL: How Efficient is LLM-Generated Code?** — https://arxiv.org/abs/2406.06647

These all evaluate runtime, not size. Useful as a model for what a hypothetical "AutoBench-Slim" — a benchmark of `(verbose_llm_output, lean_human_rewrite)` pairs scored on LOC, complexity, and abstraction count alongside correctness — would need to look like. **No such benchmark exists today**; ShorterCodeBench (828 pairs) is the only thing in the neighborhood.

---

## 7. The gap

There is no published benchmark or canonical dataset for "real LLM-generated code paired with senior-engineer rewrites scored on size + correctness." ShorterCodeBench is HumanEval-flavored, not real-world. The practitioner literature (sections 4 above) catalogs the patterns but doesn't quantify their prevalence. The static-analysis literature (section 1) wasn't designed with LLM output in mind and over-flags on idiomatic human code while under-flagging on plausible-looking LLM scaffolding.

This gap is worth noting because it constrains what AutoDev can validate against. Any anti-bloat capability AutoDev ships will, for now, have to be evaluated on:

1. Internal regression fixtures (curated examples of LLM bloat from real AutoDev runs).
2. The ShorterCodeBench pairs as a sanity check.
3. Longitudinal tracking of metrics on AutoDev's own codebase (size, complexity, abstraction count over time).

A rigorous external benchmark is not available to validate against.

---

## Summary of where each category fits architecturally

| Category | Natural integration point in AutoDev |
|---|---|
| Static analysis tools (§1) | New QA gate plugin via `entry_points` |
| LLM brevity research (§2) | Reviewer / critic_t / judge prompt design + few-shot exemplars |
| LLM-as-judge rubrics (§3) | Tournament judge prompt + new scoring dimension |
| Anti-pattern catalogs (§4) | Reviewer / critic_t prompt checklist + `knowledge.jsonl` seed entries |
| Existing review tools (§5) | Reference implementations for plugin protocol shape |
| Performance optimization (§6) | Reuse evaluation-harness patterns, not the optimization targets |

Hand-off note for implementation: the highest-confidence sources are §1 (well-established tools), §3 (well-established methodology), and §4 (concrete enumeration of patterns). §2 is research-stage and should be treated as informative rather than authoritative.
