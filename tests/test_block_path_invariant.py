"""v0.42.1 F1d — the by-construction autonomy guarantee, enforced in CI.

These invariant tests scan the ``src/`` tree (AST, not import) and FAIL if:

  1. any ``update_task_status(..., "blocked")`` transition is committed OUTSIDE
     ``orchestrator.blocker_guard.block_task`` — i.e. a task could reach a
     terminal ``blocked`` state without first being routed through the Universal
     Blocker Resolver (the Run-5 "resolver fired 0×" / silent dead-end class);

  2. a degrade-capable phase stops routing its fail-safe degrade through
     ``record_phase_degrade`` — i.e. a phase could silently degrade without a
     resolver breadcrumb.

Mirrors the ``task_state.assert_transition`` invariant pattern: make the unsafe
thing impossible to *add* in future, rather than relying on reviewer vigilance.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

# The single sanctioned home of the ``blocked`` commit (F1a).
_ALLOWED_BLOCK = {("orchestrator/blocker_guard.py", "block_task")}

# Every phase whose fail-safe path degrades MUST route through
# ``record_phase_degrade`` (F1b). Removing the call breaks the guarantee.
_DEGRADE_CAPABLE_PHASES = [
    "orchestrator/intake_phase.py",
    "orchestrator/diagnosis_phase.py",
    "orchestrator/framing_phase.py",
    "orchestrator/plan_phase.py",
    "orchestrator/plan_phase_recovery.py",
]


def _enclosing_func(func_ranges: list[tuple[int, int, str]], lineno: int) -> str:
    best: tuple[int, str] | None = None
    for start, end, name in func_ranges:
        if start <= lineno <= end and (best is None or start > best[0]):
            best = (start, name)
    return best[1] if best else "<module>"


def _block_sites() -> list[tuple[str, int, str]]:
    """Return (relpath, lineno, enclosing_func) for every blocked-transition call."""
    sites: list[tuple[str, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        func_ranges = [
            (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.attr
                if isinstance(fn, ast.Attribute)
                else (fn.id if isinstance(fn, ast.Name) else None)
            )
            if name != "update_task_status":
                continue
            status = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                status = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "status" and isinstance(kw.value, ast.Constant):
                    status = kw.value.value
            if status == "blocked":
                rel = path.relative_to(_SRC).as_posix()
                sites.append((rel, node.lineno, _enclosing_func(func_ranges, node.lineno)))
    return sites


def test_only_block_task_commits_blocked_transition() -> None:
    offenders = [
        (rel, ln, fn)
        for rel, ln, fn in _block_sites()
        if (rel, fn) not in _ALLOWED_BLOCK
    ]
    assert not offenders, (
        "Every `update_task_status(..., \"blocked\")` must go through "
        "blocker_guard.block_task (v0.42.1 F1a). Direct block sites found:\n"
        + "\n".join(f"  {rel}:{ln} in {fn}()" for rel, ln, fn in offenders)
    )


def test_block_task_itself_is_the_single_committer() -> None:
    # The guarantee is only meaningful if block_task actually contains the commit.
    sites = _block_sites()
    assert ("orchestrator/blocker_guard.py", "block_task") in {
        (rel, fn) for rel, _, fn in sites
    }, "block_task must itself commit the blocked transition"


def test_degrade_capable_phases_route_through_record_phase_degrade() -> None:
    missing: list[str] = []
    for rel in _DEGRADE_CAPABLE_PHASES:
        src = (_SRC / rel).read_text()
        if "record_phase_degrade(" not in src:
            missing.append(rel)
    assert not missing, (
        "Every degrade-capable phase must route its fail-safe degrade through "
        "record_phase_degrade (v0.42.1 F1b). Missing in:\n"
        + "\n".join(f"  {rel}" for rel in missing)
    )
