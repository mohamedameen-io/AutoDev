# Agents

AutoDev uses 14 specialist agents: 10 from the opencode-swarm hub-and-spoke design plus 4 tournament-specific roles. This document describes each agent, the prompt rendering pipeline, and the tool map.

---

## Agent Table

| Name | Source | Role | Claude Code Model | Cursor Model |
|---|---|---|---|
| `architect` | opencode-swarm `src/agents/architect.ts` | Plan drafting, delegation decisions | opus | opus (→auto fallback) |
| `explorer` | `src/agents/explorer.ts` | Codebase reconnaissance — reads files, maps structure | haiku | auto |
| `domain_expert` | `src/agents/sme.ts` | Domain research — technology choices, patterns, risks | sonnet | sonnet |
| `developer` | `src/agents/coder.ts` | Writes code; anti-hallucination protocol | sonnet | auto |
| `reviewer` | `src/agents/reviewer.ts` | Correctness + architecture review | sonnet | sonnet |
| `test_engineer` | `src/agents/test-engineer.ts` | Tests + coverage | sonnet | auto |
| `critic_sounding_board` | `src/agents/critic-sounding-board.ts` | Escalation fork after retry exhaustion | sonnet | auto |
| `critic_drift_verifier` | `src/agents/critic-drift-verifier.ts` | Post-phase plan-vs-reality drift check | sonnet | sonnet |
| `docs` | `src/agents/docs.ts` | Post-phase documentation | sonnet | sonnet |
| `designer` | `src/agents/designer.ts` | UI/UX review (opt-in) | sonnet | sonnet |
| `critic_t` | autoreason `experiments/v2/run_overnight.py` | Plan-gate + Tournament critic — finds problems, no fixes | sonnet | sonnet |
| `architect_b` | autoreason `experiments/v2/run_overnight.py` | Tournament revision agent | sonnet | opus (→auto fallback) |
| `synthesizer` | autoreason `experiments/v2/run_overnight.py` | Tournament synthesis (randomized X/Y labels) | sonnet | sonnet |
| `judge` | autoreason `experiments/v2/run_overnight.py` | Tournament judge — ranks A/B/AB | sonnet | sonnet |

> **Note**: For Cursor, roles with "→auto fallback" will automatically switch to `auto` mode when rate limited (429 errors), ensuring graceful degradation. The `auto` mode lets Cursor intelligently select the best model per-task.

> **Note**: `judge` covers both the plan and implementation tournament judge roles. The same prompt is used for both; the content handler provides the appropriate context. `critic_t` serves dual duty as both the plan-gate (returning APPROVED/NEEDS_REVISION/REJECTED) and the tournament critic role.

---

## Agent Roles in Detail

### Hub Agents (Orchestrator-Driven)

**`architect`** — The reasoning hub. Called for plan drafting, revision after critic_t feedback, and escalation decisions. The Python FSM handles deterministic flow; the architect handles judgment calls. Uses opus for highest reasoning quality.

**`critic_t`** — Serves dual roles:
- **Plan-gate**: Reads the final tournament-refined plan and returns one of:
  - `APPROVED` — plan is ready for execution
  - `NEEDS_REVISION` — specific issues identified; architect revises and re-gates (bounded retries)
  - `REJECTED` — fundamental problems; requires human intervention
- **Tournament critic**: Reads the current version (plan or diff) and identifies problems without proposing fixes. Separation of concerns: finding problems vs. fixing them.

### Specialist Agents (Serial Execution)

**`explorer`** — Reconnaissance agent. Reads the codebase to understand structure, existing patterns, and relevant files. Uses haiku for cost efficiency (read-only, no code generation).

**`domain_expert`** — Subject matter expert. Researches technology choices, identifies risks, and provides domain context for the architect's plan.

**`developer`** — The primary implementation agent. Writes code following the anti-hallucination protocol: reads files before editing, verifies import paths, uses exact function names found in the codebase. Multi-turn capable (up to `max_turns` tool calls).

**`reviewer`** — Reviews the developer's diff for correctness, architecture alignment, and code quality. Returns structured feedback; failures trigger developer retry.

**`test_engineer`** — Writes tests and runs the test suite. Produces test evidence including pass/fail counts and coverage.

**`critic_sounding_board`** — Escalation agent. Called when QA retry limit is exhausted. May recommend re-planning, task decomposition, or blocking.

**`critic_drift_verifier`** — Post-phase drift check. Compares what was actually implemented against the plan to detect scope creep or missed requirements.

**`docs`** — Documentation agent. Called after phase completion to update or create documentation.

**`designer`** — UI/UX review agent. Opt-in; called for tasks involving user interfaces.

### Tournament Agents

**`critic_t`** — (See above, also used in tournaments) Tournament critic. Reads the current version (plan or diff) and identifies problems without proposing fixes.

**`architect_b`** — Tournament revision agent. Reads the task, the current version (A), and the critic's feedback, then proposes a revised version (B).

**`synthesizer`** — Tournament synthesis agent. Reads the task and two versions (labeled X and Y, randomized to prevent position bias) and produces a synthesis (AB) that takes the best parts of each.

**`judge`** — Tournament judge. Reads the task and three versions (A, B, AB in randomized order) and produces a ranking `RANKING: 1, 2, 3`. Multiple judges run in parallel; their rankings are aggregated via Borda scoring.

---

## Prompt Rendering Pipeline

Swarm agent prompts are stored as Markdown files in `autodev/agents/prompts/<name>.md`. They are vendored from opencode-swarm with modifications:

1. **Vendoring**: Prompts are copied verbatim from `opencode-swarm/src/agents/*.ts` (the TypeScript string content).
2. **Stripping**: `@agent` delegation syntax is removed (AutoDev's Python layer handles delegation, not the LLM).
3. **Template resolution**: `{{QA_RETRY_LIMIT}}`, `{{SWARM_ID}}`, and similar placeholders are resolved at render time using the project config.
4. **Platform rendering**:
   - Claude Code: `render_claude.py` writes `.claude/agents/<name>.md` with YAML frontmatter (`name`, `description`, `tools`, `model`)
   - Cursor: `render_cursor.py` writes `.cursor/rules/<name>.mdc` with frontmatter (`description`, `alwaysApply`)

Tournament role prompts (`critic_t`, `architect_b`, `synthesizer`, `judge`) are stored in `autodev/tournament/prompts.py` as string constants and rendered at runtime.

### Claude Code Agent File Format

```markdown
---
name: developer
description: Writes code; anti-hallucination protocol
tools: Read, Edit, Write, Bash, Glob, Grep
model: sonnet
---

You are the Developer agent. Your job is to implement exactly what the task specifies...
```

### Cursor Rule File Format

```markdown
---
description: Developer agent — writes code with anti-hallucination protocol
alwaysApply: false
---

You are the Developer agent. Your job is to implement exactly what the task specifies...
```

---

## Tool Map

The tool map (`autodev/agents/tool_map.py`) defines which Claude Code built-in tools each agent is allowed to use. This collapses opencode-swarm's ~60 plugin-specific tools to 8 Claude Code built-ins.

| Agent | Allowed Tools |
|---|---|
| `architect` | Read, Glob, Grep, WebSearch, WebFetch, Task |
| `explorer` | Read, Glob, Grep |
| `domain_expert` | Read, Glob, Grep, WebSearch, WebFetch |
| `developer` | Read, Edit, Write, Bash, Glob, Grep |
| `reviewer` | Read, Glob, Grep |
| `test_engineer` | Read, Edit, Write, Bash, Glob, Grep |
| `critic_sounding_board` | Read, Glob, Grep |
| `critic_drift_verifier` | Read, Glob, Grep |
| `docs` | Read, Edit, Write, Glob, Grep |
| `designer` | Read, Glob, Grep, WebFetch |
| `critic_t` | (none — text-only) |
| `architect_b` | (none — text-only) |
| `synthesizer` | (none — text-only) |
| `judge` | (none — text-only) |

> **Note**: The prompts still reference old opencode-swarm tool names in prose (informational only). AutoDev's Python orchestrator handles the actual QA gates and evidence collection — the agents don't need to call those tools directly.

---

## Per-Role Model Configuration

Each agent's model can be overridden in `.autodev/config.json`:

```jsonc
"agents": {
  "architect":    {"model": "opus"},    // highest reasoning quality
  "developer":    {"model": "sonnet"},  // balanced quality/cost
  "explorer":     {"model": "haiku"},   // read-only, cost-efficient
  "judge":        {"model": "sonnet"},  // tournament judge
  // ...
}
```

To disable an agent entirely (e.g., `designer` for non-UI projects):
```jsonc
"agents": {
  "designer": {"disabled": true}
}
```

---

## Adding a Custom Agent

Custom agents can be added via the plugin system:

```python
# In your package's entry_points:
[project.entry-points."autodev.plugins"]
my_agent = "my_package.agents:MyAgentExtension"
```

See [the plugins section in architecture.md](architecture.md#h-plugins-autodevplugins) for details.
