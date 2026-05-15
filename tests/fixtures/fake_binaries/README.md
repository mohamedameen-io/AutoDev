# Fake LLM Binaries

Drop-in stand-ins for `claude`, `cursor` (a.k.a. `cursor-agent`), and
`pytest` that the AutoDev orchestrator can shell out to during E2E tests
without making any network calls or real test runs.

All three scripts are pure POSIX bash — no Python, no Node, no `jq` — so
they work identically on macOS and Ubuntu CI runners.

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
- **fake-pytest**:
  `RESULTS: passed=3 failed=0 total=3` on stdout, exit 0.

The fake-pytest "prompt" used for canned lookup is the joined non-flag
positional argv (typically the test path or nodeid).

## Failure-mode switch

Set `AUTODEV_FAKE_FAILURE_MODE` to short-circuit before the canned lookup:

| Mode                                     | Behaviour                                                                                                         | Applies to        |
|------------------------------------------|-------------------------------------------------------------------------------------------------------------------|-------------------|
| `error_max_turns`                        | Print `error_max_turns` JSON, exit 1                                                                              | fake-claude       |
| `empty_result`                           | Print `{"result":""}`, exit 0                                                                                     | both LLMs         |
| `reviewer_returns_empty_silently`        | Alias for `empty_result` (named after the scenario it asserts)                                                    | fake-claude       |
| `timeout`                                | `sleep 30` (rely on the harness's `wait_for` to kill us)                                                          | both LLMs         |
| `nonzero_exit`                           | Write to stderr, exit 3                                                                                           | both LLMs         |
| `usage_limit`                            | Emit usage-limit JSON to stderr, exit 1                                                                           | fake-cursor       |
| `is_error_true_with_empty_result`        | Print `{"is_error":true,"result":""}`, exit 0 (v0.31.1 dump-predicate regression guard)                           | fake-cursor       |
| `architect_rejected_paths_{1,2,3}`       | Return a plan referencing paths the validator rejects; bumps `$AUTODEV_FAKE_RESPONSE_DIR/.attempt_count` per call | fake-claude       |
| `repetition_loop`                        | Return IDENTICAL output every call regardless of prompt                                                           | fake-claude       |
| `no_tests_collected`                     | `collected 0 items`, exit 5 (pytest's "no tests" code)                                                            | fake-pytest       |
| `zero_pass_zero_fail`                    | `RESULTS: passed=0 failed=0 total=0`, exit 0 (silent zero — Gap C)                                                | fake-pytest       |
| `collection_error`                       | `SyntaxError` to stderr, exit 1                                                                                   | fake-pytest       |
| `runtime_crash`                          | `ImportError` + traceback to stderr, exit 1                                                                       | fake-pytest       |
| `capture_failed`                         | Empty stdout AND stderr, exit 1                                                                                   | fake-pytest       |

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
