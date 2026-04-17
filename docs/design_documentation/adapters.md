# Platform Adapters

AutoDev supports two platforms: **Claude Code** (`claude -p`) and **Cursor** (`cursor agent --print`). Both are wrapped behind a uniform `PlatformAdapter` protocol so the orchestrator and tournament engine are platform-agnostic.

---

## Platform Adapter Contract

```python
class PlatformAdapter(Protocol):
    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        """Write platform-native agent files (called by autodev init)."""
        ...

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        """Invoke a single agent and return its result."""
        ...

    async def parallel(
        self, invs: list[AgentInvocation], max_concurrent: int = 3
    ) -> list[AgentResult]:
        """Invoke multiple agents concurrently (used for tournament judges)."""
        ...

    async def healthcheck(self) -> bool:
        """Return True if the platform CLI is available and logged in."""
        ...
```

### AgentInvocation

```python
class AgentInvocation(BaseModel):
    role: str                       # "developer", "critic_t", "judge", etc.
    prompt: str                     # fully rendered prompt (no templating inside adapter)
    cwd: Path                       # working directory for the subprocess
    model: str | None = None        # Claude Code: "sonnet" | "opus" | "haiku" | None (use default) | Cursor: "auto" | "sonnet" | "opus"
    timeout_s: int = 600            # subprocess timeout
    allowed_tools: list[str] | None = None   # passed as --allowed-tools
    max_turns: int = 1              # most roles = single-turn; developer can be multi-turn
```

### AgentResult

```python
class AgentResult(BaseModel):
    success: bool
    text: str                       # final message text
    tool_calls: list[ToolCall]
    files_changed: list[Path]
    diff: str | None
    duration_s: float
    error: str | None
    raw_stdout: str                 # for debugging
```

---

## Claude Code Adapter

**File**: `autodev/adapters/claude_code.py`

### Subprocess Invocation

```bash
claude -p "<rendered_prompt>" \
  --output-format json \
  --model <model> \
  --permission-mode acceptEdits \
  --allowed-tools Read,Edit,Bash,Glob,Grep
```

The subprocess is launched with `cwd=<repo_path>` (Python's `subprocess.run` parameter).

### Agent Files

`autodev init` writes `.claude/agents/<role>.md` with YAML frontmatter:

```markdown
---
name: developer
description: Writes code; anti-hallucination protocol
tools: Read, Edit, Write, Bash, Glob, Grep
model: sonnet
---

<prompt content>
```

### Known Deviations

| Deviation | Details |
|---|---|
| **No `--cwd` flag** | The `claude` CLI does not accept a `--cwd` argument. AutoDev uses Python's `subprocess.run(cwd=...)` parameter instead. |
| **No temperature control** | `claude -p` does not expose per-call temperature. The tournament relies on **fresh-context-per-call** (subprocess isolation) for stochasticity instead of temperature variation. |
| **`--continue` not used** | Each call is a fresh context. Continuity lives in `.autodev/` state, not in the LLM session. |
| **`--permission-mode acceptEdits`** | Required for the developer and test_engineer roles to write files. |

### Output Parsing

The adapter parses the JSON output from `claude -p --output-format json`:
- `result` field → `AgentResult.text`
- `toolUseBlocks` → `AgentResult.tool_calls`
- File change detection via git diff after the call

---

## Cursor Adapter

**File**: `autodev/adapters/cursor.py`

### Subprocess Invocation

```bash
cursor agent "<rendered_prompt>" --print --output-format json --model <model>
```

Falls back to `cursor-agent` binary name if `cursor` is not found on PATH.

### Agent Files

`autodev init` writes `.cursor/rules/<role>.mdc`:

```markdown
---
description: Coder agent — writes code with anti-hallucination protocol
alwaysApply: false
---

<prompt content>
```

### Known Deviations

| Deviation | Details |
|---|---|
| **No native tool restriction** | Cursor has no `--allowed-tools` equivalent. AutoDev relies on `.cursor/rules/*.mdc` prompt-level constraints to guide tool use. |
| **No temperature control** | Same as Claude Code — fresh-context-per-call provides stochasticity. |
| **Rate limit fallback** | For explicit `opus` or `sonnet` models, the adapter automatically falls back to `auto` when rate limited (429 error or "rate limit" in stderr). This ensures high-reasoning roles gracefully degrade to auto-select mode. |

---

## Auto-Detection Logic

**File**: `autodev/adapters/detect.py`

Adapter selection follows this precedence:

1. **CLI flag** `--platform claude|cursor|auto`
2. **Environment variable** `AUTODEV_PLATFORM=claude_code|cursor|auto`
3. **Config file** `config.json.platform` (if not `"auto"`)
4. **Auto-detect**:
   - Run `claude --version` → if exit 0, use `ClaudeCodeAdapter`
   - Else run `cursor --version` → if exit 0, use `CursorAdapter`
   - Else fail with diagnostic: "No supported CLI found. Run `autodev doctor`."

```python
async def get_adapter(platform: str) -> PlatformAdapter:
    """Resolve and return the appropriate platform adapter."""
    ...
```

---

## Subprocess Invocation Patterns

All subprocess calls share these properties:

- **Stateless**: each call is a fresh subprocess with no session continuity
- **Async**: `asyncio.create_subprocess_exec` for non-blocking execution
- **Timeout**: configurable per-invocation via `AgentInvocation.timeout_s` (default 600s)
- **Retry**: `AdapterLLMClient` wraps the adapter with tenacity retry on `TransientError` (rate limits, transient failures)
- **Parallel**: `adapter.parallel()` uses `asyncio.gather` capped at `max_parallel_subprocesses`

### Retry Configuration

```python
# In autodev/tournament/llm.py
class AdapterLLMClient:
    def __init__(
        self,
        adapter: PlatformAdapter,
        cwd: Path,
        timeout_s: int = 600,
        max_attempts: int = 3,
    ): ...
```

Retries use exponential backoff on `TransientError`. Permanent errors (e.g., invalid prompt, auth failure) are not retried.

---

## Adding a New Adapter

To add support for a new platform:

1. Create `autodev/adapters/<platform>.py` implementing the `PlatformAdapter` protocol.
2. Add detection logic to `autodev/adapters/detect.py`.
3. Add agent file rendering to `autodev/agents/render_<platform>.py`.
4. Register the platform in `autodev/config/schema.py` (`platform` literal type).
5. Add tests in `tests/test_adapter_<platform>.py`.
