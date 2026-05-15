# Contributing to AutoDev

## Development setup

```bash
uv sync --all-extras
uv run pytest tests/ -q
```

The fast unit-test loop above excludes the slower integration suite under
`tests/integration/`. Those run automatically in CI on every PR (see
`.github/workflows/test.yml`).

## Releasing

1. Update `CHANGELOG.md` with a `## [X.Y.Z]` section describing the
   release. Use the existing entries as a template.
2. Run the version-bump validator:
   ```bash
   python scripts/release_version.py X.Y.Z
   ```
   This validates semver formatting, the CHANGELOG entry, and the
   `npm/package.json` version (use `--allow-npm-mismatch` while the npm
   package is intentionally decoupled). On success it rewrites
   `src/_version.py`.
3. Commit:
   ```bash
   git commit -am "chore(release): bump to X.Y.Z"
   ```
4. Push to `main`:
   ```bash
   git push origin main
   ```
5. From the GitHub Actions tab, manually trigger the **Release**
   workflow (`.github/workflows/release.yml`) with `version=X.Y.Z`.
6. Watch each gate go green, in order:
   - **preflight-checks** — version + changelog + slash template drift
     + reviewer prompt sanity
   - **unit-tests** — full pytest run including the fake-binary E2E
   - **doctor-smoke** — built wheel installs and `autodev doctor` runs
   - **manual-smoke-issue** — opens a GitHub issue with the manual
     checklist (do not close until step 7 is done)
   - **tag-and-publish** — creates and pushes the `vX.Y.Z` tag,
     which triggers `publish.yml` to push to PyPI + npm
   - **post-publish-smoke** — installs the published wheel from PyPI
     30 s after the tag and runs `autodev --version` + `autodev doctor`
7. Walk the manual smoke checklist issue end to end, then close it.

If any gate fails, fix on `main` (or revert the bump commit) before
re-triggering. Never publish around a red gate.

## Fake LLM binaries for tests

`tests/fixtures/fake_binaries/` ships pure-bash stand-ins for `claude`
and `cursor` that respect a canned-response protocol and an
`AUTODEV_FAKE_FAILURE_MODE` env switch. See
[tests/fixtures/fake_binaries/README.md](tests/fixtures/fake_binaries/README.md)
for the protocol. The integration tests at
`tests/integration/test_e2e_with_fake_binaries.py` show how to wire
them up.
