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

# Phase 1A Step 6 (RECOVERY-CONTRACT §7 Step 6; gate R5): the F1d gate originally
# scanned only ``ast.Call`` to ``update_task_status(..., "blocked")`` — it was
# BLIND to a direct ``t.status = "blocked"`` ``ast.Assign`` (the cascade-block
# bypass, ``WS3-block-path-invariant-coverage-gap``). These are the ONLY
# sanctioned direct-assign sites, by enclosing function:
#   * the live cascade (``mark_blocked_descendants``) — which MUST emit a
#     ``mark_blocked_descendants`` breadcrumb (Step 1 / R1);
#   * the two ledger-REPLAY reconstructors (``_apply_op`` / ``_apply_for_load``)
#     which re-apply already-recorded transitions, not NEW block decisions.
# Any other direct ``.status = "blocked"`` assignment fails the gate.
_ALLOWED_DIRECT_ASSIGN = {
    ("state/plan_manager.py", "mark_blocked_descendants"),  # cascade (breadcrumbed)
    ("state/ledger.py", "_apply_op"),  # full-replay reconstructor
    ("state/plan_manager.py", "_apply_for_load"),  # snapshot fast-path replay
}

# Variable-routed ``update_task_status(id, <non-constant>)`` sites where the
# status arg is NOT a literal — the gate cannot prove the value is not
# "blocked", so each must be a KNOWN-SAFE setter (the value is provably never
# "blocked"). New variable-routed status writers must be added here consciously.
_ALLOWED_VARIABLE_ROUTED_STATUS = {
    ("orchestrator/blocker_guard.py", "block_task"),  # the sanctioned committer
    # target_status = "in_progress" if status != "in_progress" else status
    # → the ternary ALWAYS yields "in_progress"; never "blocked".
    ("orchestrator/execute_phase.py", "_resolver_retry"),
    ("orchestrator/execute_phase.py", "_dispatch_architect_consult"),
    # iterates a FIXED slice of the pipeline tuple ("coded","auto_gated",
    # "reviewed","tested","tournamented") then literal "complete" — "blocked" is
    # never in it. The shared FSM-walk-to-complete primitive (Tier J's
    # accept-approved-on-exhaustion + WS5's best-effort-commit both route their
    # completion through it).
    ("orchestrator/execute_phase.py", "_walk_task_to_complete"),
    # idempotent SELF-write of the current status to clear resolver_note at the
    # re-enable loop-top (status is in_progress there); preserves status, so it
    # can never CREATE a blocked transition.
    ("orchestrator/execute_phase.py", "_execute_one"),
}

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


def _block_assign_sites() -> list[tuple[str, int, str]]:
    """Return (relpath, lineno, enclosing_func) for every direct
    ``<x>.status = "blocked"`` ``ast.Assign`` (the cascade-block bypass class).
    """
    sites: list[tuple[str, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        func_ranges = [
            (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = node.value
            if not (isinstance(val, ast.Constant) and val.value == "blocked"):
                continue
            for tgt in targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "status":
                    rel = path.relative_to(_SRC).as_posix()
                    sites.append(
                        (rel, node.lineno, _enclosing_func(func_ranges, node.lineno))
                    )
    return sites


def _variable_routed_status_sites() -> list[tuple[str, int, str]]:
    """Return sites where ``update_task_status`` is called with a NON-constant
    status argument (positional [1] or kw ``status``) — the gate cannot prove
    the value is not "blocked", so each must be a known-safe setter.
    """
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
            status_arg: ast.expr | None = None
            if len(node.args) >= 2:
                status_arg = node.args[1]
            for kw in node.keywords:
                if kw.arg == "status":
                    status_arg = kw.value
            if status_arg is None:
                continue  # status defaulted elsewhere; not a direct write here
            if isinstance(status_arg, ast.Constant):
                continue  # literal — covered by _block_sites
            rel = path.relative_to(_SRC).as_posix()
            sites.append(
                (rel, node.lineno, _enclosing_func(func_ranges, node.lineno))
            )
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


def test_only_whitelisted_sites_directly_assign_blocked() -> None:
    """Gate R5 (Step 6): no NEW ``t.status = "blocked"`` ``ast.Assign`` outside the
    sanctioned cascade + replay sites. Closes the cascade-block bypass that the
    Call-only F1d gate was blind to."""
    sites = _block_assign_sites()
    # Anti-vacuity: the scanner must actually find the known sites (a regex/AST
    # regression that finds nothing must NOT pass vacuously).
    assert len(sites) >= 4, (
        f"F1d direct-assign scanner found too few sites ({len(sites)}) — it is "
        "likely broken (vacuous). Expected the cascade + replay reconstructors."
    )
    offenders = [
        (rel, ln, fn)
        for rel, ln, fn in sites
        if (rel, fn) not in _ALLOWED_DIRECT_ASSIGN
    ]
    assert not offenders, (
        "Every direct `<x>.status = \"blocked\"` assignment must be the cascade "
        "(which emits a breadcrumb) or a ledger-replay reconstructor (Step 6 / "
        "F1d). Unsanctioned direct block-assign sites found:\n"
        + "\n".join(f"  {rel}:{ln} in {fn}()" for rel, ln, fn in offenders)
    )


def test_cascade_assign_site_emits_breadcrumb() -> None:
    """The one LIVE direct-assign site (the cascade) must emit a typed breadcrumb
    so it is not a silent dead-end (R1 + R5)."""
    src = (_SRC / "state/plan_manager.py").read_text()
    # The cascade function commits descendants directly; it MUST append the
    # typed ``mark_blocked_descendants`` op (the cascade breadcrumb).
    assert 'op="mark_blocked_descendants"' in src, (
        "the cascade-block site (mark_blocked_descendants) must emit a "
        "mark_blocked_descendants ledger breadcrumb (no silent cascade dead-end)"
    )


def test_variable_routed_status_writers_are_known_safe() -> None:
    """Gate R5 (Step 6): a variable-routed ``update_task_status(id, <var>)`` could
    smuggle a "blocked" past the literal-only F1d scan. Each such site must be a
    known-safe setter whose value is provably never "blocked"."""
    offenders = [
        (rel, ln, fn)
        for rel, ln, fn in _variable_routed_status_sites()
        if (rel, fn) not in _ALLOWED_VARIABLE_ROUTED_STATUS
    ]
    assert not offenders, (
        "A variable-routed update_task_status() status write was found outside "
        "the known-safe set — it could route a 'blocked' transition past the "
        "F1d literal scan. Add it to _ALLOWED_VARIABLE_ROUTED_STATUS only if its "
        "value is provably never 'blocked':\n"
        + "\n".join(f"  {rel}:{ln} in {fn}()" for rel, ln, fn in offenders)
    )


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
