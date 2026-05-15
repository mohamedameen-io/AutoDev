# Fake LLM Binaries

Drop-in stand-ins for `claude` and `cursor` (a.k.a. `cursor-agent`) that the
AutoDev orchestrator can shell out to during E2E tests without making any
network calls.

Both scripts are pure POSIX bash — no Python, no Node, no `jq` — so they
work identically on macOS and Ubuntu CI runners.

## Usage

```bash
export PATH="$PWD/tests/fixtures/fake_binaries:$PATH"
export AUTODEV_FAKE_RESPONSE_DIR="$(mktemp -d)"
autodev plan "..."
```

`PATH`-prepending is enough to make the orchestrator pick the fakes up — the
real adapter just spawns whichever `claude` / `cursor` resolves first.

## Canned response protocol

For each prompt the harness wants to script:

1. md5-hash the *exact* prompt string (UTF-8, no trailing newline).
2. Drop a JSON file at
   `${AUTODEV_FAKE_RESPONSE_DIR}/response_<hash>.json`.

When called, the fake hashes the prompt it receives (`md5sum` on Linux,
`md5 -q` on macOS), looks the canned file up, and `cat`s it to stdout.
If no file matches, it emits a generic success blob:

- **fake-claude**:
  `{"result":"[fake-claude] default","model":"claude-haiku","stop_reason":"end_turn","usage":{"input_tokens":100,"output_tokens":50}}`
- **fake-cursor**:
  `{"result":"[fake-cursor] default","thread_id":"fake-thread","is_error":false}`

## Failure-mode switch

Set `AUTODEV_FAKE_FAILURE_MODE` to short-circuit before the canned lookup:

| Mode             | Behaviour                                                     | Applies to        |
|------------------|---------------------------------------------------------------|-------------------|
| `error_max_turns`| Print `error_max_turns` JSON, exit 1                          | fake-claude       |
| `empty_result`   | Print `{"result":""}`, exit 0                                 | both              |
| `timeout`        | `sleep 30` (rely on the harness's `wait_for` to kill us)      | both              |
| `nonzero_exit`   | Write to stderr, exit 3                                       | both              |
| `usage_limit`    | Emit usage-limit JSON to stderr, exit 1                       | fake-cursor       |

Unset / unrecognised → normal canned-response flow.

## Recognised flags

These exist purely so the fake doesn't choke when the real adapter passes
them; values are dropped on the floor.

- **fake-claude**: `-p` / `--prompt`, `--output-format`, `--effort`,
  `--max-turns`, `--allowed-tools`, `--permission-mode`, `--model`,
  `--version`.
- **fake-cursor**: leading `agent <prompt>` positional, `--print`,
  `--output-format`, `--force`, `--model`, `--version`.

If `-p` / the leading positional prompt is missing the fake reads stdin,
matching the real CLIs' piped-prompt behaviour.
