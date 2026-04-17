# Examples

This directory contains example projects that demonstrate AutoDev's capabilities. Each example includes a spec, source files, and an AutoDev configuration.

---

## How to Use

1. Copy an example directory to a new location (or use it in place):
   ```bash
   cp -r examples/subtract /tmp/my-subtract-project
   cd /tmp/my-subtract-project
   ```

2. Initialize AutoDev (if not already initialized):
   ```bash
   autodev init
   ```

3. Run the plan phase with the spec as intent:
   ```bash
   autodev plan "$(cat .autodev/spec.md)"
   ```

4. Execute the plan:
   ```bash
   autodev execute
   ```

5. Check status:
   ```bash
   autodev status
   ```

---

## Examples

### `subtract/` — Tiny Repo

A minimal Python project with a single `math.py` file. The spec asks AutoDev to add a `subtract(a, b)` function alongside the existing `add(a, b)`.

**Purpose**: Demonstrates the basic plan → execute flow on a trivially small codebase. Good for testing your AutoDev setup without spending much subscription quota.

**Files**:
- `math.py` — existing `add(a, b)` function
- `__init__.py` — package init
- `.autodev/spec.md` — intent: add subtract function
- `.autodev/config.json` — minimal config with tournaments disabled for cost

**Expected outcome**: autodev plans one task, the coder adds `subtract(a, b)`, tests are written, and the task completes.

---

### `jwt_auth/` — JWT Authentication

A realistic spec for building JWT-based authentication in a Python web service. This example demonstrates AutoDev on a multi-task plan with real complexity.

**Purpose**: Shows how AutoDev handles a non-trivial spec with multiple phases (models, routes, middleware, tests). The plan tournament is particularly useful here — the architect's initial draft often misses edge cases that the tournament refines.

**Files**:
- `spec.md` — detailed requirements for JWT auth
- `README.md` — expected plan structure and sample output explanation

**Expected outcome**: AutoDev produces a 3–5 task plan covering token generation, validation middleware, protected routes, and tests.

---

## Tips

- Run `autodev doctor` first to verify your CLI setup.
- Use `autodev tournament --phase=plan --input .autodev/spec.md --dry-run` to test the tournament without LLM calls.
- The `subtract/` example has tournaments disabled in its config — re-enable them in `.autodev/config.json` to see the full tournament flow.
- After execution, inspect `.autodev/tournaments/` to see the tournament artifacts and per-pass history.
