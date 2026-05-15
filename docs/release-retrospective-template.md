# Release Retrospective: vX.Y.Z

**Date:** YYYY-MM-DD
**Release tag:** vX.Y.Z
**Postmortem author:** <name>
**Triggering event:** <e.g. "fresh production run on a representative customer codebase">

> Copy this file to `docs/retrospectives/vX.Y.Z.md` and fill in every section.
> Section 5 (`What's the NEXT layer of failure?`) is **required** and is enforced
> by the release CI gate. Without at least 3 candidates the release will not pass.

## 1. What shipped
Per-phase verification status. For each phase in the release plan:

- Phase 1: [ landed | partial | deferred ] — short note on what was deferred and why.
- Phase 2: [ landed | partial | deferred ] — short note.
- Phase 3: ...
- ...

## 2. What broke in the field
Per-finding severity matrix. Use the same format as prior retrospectives.

| ID | Finding | Severity | First-seen-version | Code anchor |
|---|---|---|---|---|
| F-1 | <short description> | Blocker / High / Medium / Low | vX.Y.Z | path/to/file.py:LINE |

## 3. Per-fix verdict
For each phase that was supposed to fix something, did it actually engage as designed?

| Fix | Engaged? | Working? | Evidence |
|---|---|---|---|
| Phase 1 (<name>) | yes / no / partial | yes / no / unknown | <log line, test, run id, ...> |

## 4. What's NEW
Failure modes that weren't on the prior retrospective's list. Be precise about whether they were:

- **Upstream-invisible** (genuinely couldn't have predicted) — e.g. a third-party API changed shape.
- **Below the most-urgent layer at the prior retrospective time** — known-but-deprioritised.
- **Caused by the fixes themselves** (regression) — the fix introduced the problem.

## 5. What's the NEXT layer of failure?
**REQUIRED SECTION.** Speculate explicitly about what will surface AFTER the v(X.Y.Z) fixes ship.
This is the input to v(X.Y.Z+1) planning. List **at least 3** candidates with severity guesses.

Format (one bullet per candidate, all four sub-fields required):

- **Candidate 1:** <description>. Severity: <Blocker | High | Medium | Low>. Why we suspect: <one-line reason>. Falsifiable signal: <what we'd see in a future run that confirms or refutes this>.
- **Candidate 2:** <description>. Severity: <...>. Why we suspect: <...>. Falsifiable signal: <...>.
- **Candidate 3:** <description>. Severity: <...>. Why we suspect: <...>. Falsifiable signal: <...>.

Optional candidates beyond 3 are encouraged.

## 6. Process / discipline notes
- Did the prior retrospective's "next-layer" predictions come true? (If yes/no, note which.)
- Anything to change about HOW we run retrospectives (template, cadence, who attends, ...).

## 7. Action items for v(X.Y.Z+1)
- <linked plan ID or section reference>
- <issue number, doc path, ...>
