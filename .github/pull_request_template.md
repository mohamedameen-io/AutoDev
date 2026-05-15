## Summary
<what changed and why>

## Type of change
- [ ] Bug fix
- [ ] Feature
- [ ] Refactor
- [ ] Docs
- [ ] Release bump (`chore(release):` — see required checklist below)
- [ ] CI / infrastructure

## Test plan
- [ ] Unit tests added/updated
- [ ] Existing tests still pass: `uv run pytest tests/ -q`
- [ ] If applicable: integration / E2E coverage

## Release-bump checklist (required for `chore(release): bump to *` PRs)
- [ ] `src/_version.py` bumped to the new version
- [ ] `CHANGELOG.md` has a new `## [X.Y.Z]` section listing every shipped phase
- [ ] **`docs/retrospectives/<prior-version>.md` exists and includes a populated `## 5. What's the NEXT layer of failure?` section with at least 3 candidates** — see `docs/release-retrospective-template.md`
- [ ] `python scripts/release_version.py X.Y.Z --check-only --allow-npm-mismatch` passes
