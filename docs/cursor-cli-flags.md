# Cursor CLI Flags Reference (as used by AutoDev)

This document records the Cursor CLI flags AutoDev relies on, how it
discovered each one, and the assumptions baked into the adapter when the
CLI does not expose a flag for a feature we need.

Last verified against: `cursor` / `cursor-agent` (output of `cursor-agent --help`,
captured 2026-05).

## Flags AutoDev passes today

| Flag                         | Purpose                                      |
|------------------------------|----------------------------------------------|
| `agent <prompt>`             | Subcommand form on the `cursor` binary       |
| `<prompt>`                   | Positional prompt on `cursor-agent`          |
| `--print` / `-p`             | Headless / non-interactive mode              |
| `--output-format json`       | Structured output for parsing                |
| `--force` / `-f`             | Bypass Workspace Trust prompt                |
| `--model <name>`             | Override the model (e.g. `auto`, `sonnet-4`) |

## Max Mode

**Status: not exposed by the public Cursor CLI as of the captured help text.**

The `cursor` and `cursor-agent` binaries shipped with the verified version
do NOT advertise any of: `--max`, `--max-mode`, `--no-max`, `--mode max`,
or `--model-tier`. The only `--mode` values the CLI accepts are `plan` and
`ask`, which control read-only/planning behaviour, not the Cursor IDE
"Max Mode" feature that controls per-call model tier.

This means Max Mode is, in practice, a property of the Cursor account /
IDE settings rather than something AutoDev can flip per invocation from
the command line. The user-visible policy "downshift to `auto` with Max
Mode disabled on usage-limit" therefore decomposes into:

1. **What we can do today:** unconditionally downshift the `--model`
   value to `auto` on a usage-limit hit. The `--model auto` request
   asks Cursor to pick the cheapest viable model server-side, which is
   the closest the CLI lets us get to "Max Mode off".
2. **What we cannot do today:** explicitly toggle Max Mode off via a
   CLI flag. The downshift relies on the user's Cursor account/IDE
   default treating `--model auto` as a non-Max request. This is the
   conservative assumption (Max Mode is opt-in in the Cursor IDE; the
   CLI inherits the same default).
3. **If Cursor adds a flag in the future:** wire it into
   `_max_mode_flag_for()` in `src/adapters/cursor.py`. The adapter
   already plumbs the tri-state `AgentInvocation.max_mode` end-to-end
   so the change is local.

### How to verify whether a future Cursor release exposes Max Mode

```bash
cursor agent --help 2>&1 | grep -iE 'max|mode|tier'
cursor-agent --help 2>&1 | grep -iE 'max|mode|tier'
```

If a flag appears, update `_max_mode_flag_for()` and remove the TODO
comment in `src/adapters/cursor.py`.

## Operator override: `AUTODEV_CURSOR_DISABLE_MAX_FALLBACK`

Setting `AUTODEV_CURSOR_DISABLE_MAX_FALLBACK=1` in the environment
disables the auto-downshift on usage / rate limit. Use this when:

- You are on an enterprise / unlimited Cursor plan and want the original
  model selection preserved through retries.
- You are debugging the downshift logic and need the raw error to surface.

When the override is set, a usage-limit signal returns the underlying
adapter error directly without a retry attempt.

## See also

- `src/adapters/cursor.py` — `_classify_limit_signal`, `_max_mode_flag_for`,
  and the downshift loop in `execute()`.
- `src/adapters/types.py` — `AgentInvocation.max_mode` tri-state.
- `src/orchestrator/circuit_breaker.py` — `usage_limit_hit` is tracked
  alongside `rate_limited` in `INFRASTRUCTURE_SUBTYPES`.
