"""Gate-closer A — G6 (language_unsupported op) + G7-detect (submodule guard).

G6: an UNSUPPORTED language (``detect_language`` returns ``None`` OR a
non-RUNNABLE language such as ``elixir``) must produce an explicit
``language_unsupported`` ledger op at the QA-gate-dispatch point, and NO gate
may vacuously PASS (passed=True) for that case. A genuinely-empty repo (no
source files at all) stays a legitimate ``no_source`` pass — distinct from an
unsupported-language-with-source repo.

G7-detect: ``detect_language``'s weighted scan walks the whole tree and is
POLLUTED by a checked-out git submodule's working tree (a submodule's
``package.json`` / ``.ts`` files can flip the HOST repo's language). The host
repo's declared language (``python``) must win — submodule paths (parsed from
``.gitmodules``; also ``.git/modules``) are excluded from the weighted scan.

Engagement-first TDD: these assert the *post-fix* contract. On HEAD (before the
fix) the G6 emission tests FAIL (no op emitted; gates vacuously pass) and the
G7 pollution test FAILS (submodule flips host python → nodejs).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingPlanManager:
    """Minimal ``plan_manager`` stub recording every ``ledger_append`` call."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, dict]] = []

    async def ledger_append(self, op: str, payload: dict | None = None):
        self.ops.append((op, dict(payload or {})))
        return None


def _make_orch(cwd: Path):
    """Build an orchestrator stub wired with a recording plan_manager."""
    from orchestrator import execute_phase as ep  # noqa: F401  (import smoke)

    class FakeCfg:
        hallucination_guard = False

        class qa_gates:
            syntax_check = True
            lint = True
            build_check = True
            test_runner = True
            secretscan = False
            secretscan_baseline_enabled = False
            secretscan_per_extension_thresholds = None
            secretscan_auto_skip_huge_repo = True
            secretscan_force_run_on_huge_repo = False
            mutation_test_enabled = False
            mutation_test_threshold = 0.7
            code_size = False
            lint_timeout_s = 120.0
            test_timeout_s = 600.0
            build_check_timeout_s = 120.0

        diagnosis = None

    pm = _RecordingPlanManager()
    orch = type(
        "OrchStub",
        (),
        {
            "cfg": FakeCfg(),
            "cwd": cwd,
            "plugin_registry": None,
            "plan_manager": pm,
            "_repo_capacity": None,
        },
    )()
    return orch, pm


class _FakeTask:
    # produces_diff=False so the gate dispatch runs with paths=[] (no diff to
    # scan) — the unsupported-language emission must NOT depend on a diff.
    id = "1.1"
    produces_diff = False
    metadata: dict = {}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(cwd: Path) -> None:
    _git(cwd, "init", "-q")
    _git(cwd, "config", "user.email", "t@t.t")
    _git(cwd, "config", "user.name", "t")


# ---------------------------------------------------------------------------
# G6: unsupported-language repo → language_unsupported op + no vacuous pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g6_unsupported_language_emits_ledger_op(tmp_path: Path) -> None:
    """An .ex-only repo (detect_language → None) emits ``language_unsupported``
    and NO gate vacuously passes (the gate-set degrades loud / skips with the
    diagnostic instead of silently returning the all-clear)."""
    from orchestrator import execute_phase as ep

    # Elixir source — not in the detection vocabulary, no recognised manifest.
    (tmp_path / "app.ex").write_text("defmodule App do\nend\n", encoding="utf-8")
    (tmp_path / "mix.exs").write_text("defmodule App.MixProject do\nend\n", encoding="utf-8")

    orch, pm = _make_orch(tmp_path)

    out = await ep._run_qa_gates(orch, _FakeTask())

    ops = [op for op, _ in pm.ops]
    assert "language_unsupported" in ops, (
        "QA-gate dispatch must emit a 'language_unsupported' ledger op for an "
        f"unsupported-language-with-source repo; got ops={ops!r}"
    )
    # Payload carries {language|None, reason}.
    payload = next(p for op, p in pm.ops if op == "language_unsupported")
    assert "language" in payload
    assert payload["language"] is None
    assert payload.get("reason")
    # Degrade-loud: the dispatch must NOT report a clean all-pass (None). It
    # surfaces the unsupported-language diagnostic as the blocking detail.
    assert out is not None, (
        "no gate may vacuously PASS for an unsupported-language repo with "
        "source — the dispatch must degrade loud, not return the all-clear"
    )
    assert "language" in out.lower() or "unsupported" in out.lower()


@pytest.mark.asyncio
async def test_g6_empty_repo_is_legit_no_source_pass(tmp_path: Path) -> None:
    """A genuinely-empty repo (no source files) stays a legitimate no_source
    pass: NO ``language_unsupported`` op, and the dispatch returns None
    (all-clear) — distinct from unsupported-language-with-source."""
    from orchestrator import execute_phase as ep

    # Empty repo: only a README, no recognised source / manifest.
    (tmp_path / "README.md").write_text("# empty\n", encoding="utf-8")

    orch, pm = _make_orch(tmp_path)

    out = await ep._run_qa_gates(orch, _FakeTask())

    ops = [op for op, _ in pm.ops]
    assert "language_unsupported" not in ops, (
        "a genuinely-empty repo (no source) must NOT emit language_unsupported "
        f"— that is the legit no_source pass; got ops={ops!r}"
    )
    assert out is None, "an empty repo legitimately passes all gates"


def test_g6_language_unsupported_op_is_audit_only_roundtrip() -> None:
    """The new op is registered and replays as an audit-only no-op (it never
    mutates plan state and is tolerated before any init_plan)."""
    from state.ledger import LedgerEntry, _apply_op

    entry = LedgerEntry(
        seq=1,
        timestamp="2026-01-01T00:00:00Z",
        session_id="s",
        op="language_unsupported",
        payload={"language": None, "reason": "no recognised language"},
        prev_hash="",
        self_hash="deadbeef",
    )
    # _apply_op does not validate hashes; plan=None (op may precede init_plan)
    # must NOT raise (audit-only, order-independent replay).
    assert _apply_op(None, entry) is None


# ---------------------------------------------------------------------------
# G7-detect: submodule must NOT pollute host-repo language detection
# ---------------------------------------------------------------------------


def _build_host_python_with_nodejs_submodule(root: Path) -> Path:
    """Host repo: python (pyproject + .py). Submodule working tree: nodejs
    (package.json + several .ts files) heavy enough to FLIP the weighted scan
    to nodejs if it is not excluded."""
    host = root / "host"
    host.mkdir()
    (host / "app.py").write_text("print(1)\n", encoding="utf-8")
    (host / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")

    # Submodule directory (checked-out working tree). Declared in .gitmodules.
    sub = host / "vendor" / "sub"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text('{"name":"sub"}\n', encoding="utf-8")
    # Enough TS/JS weight to outscore the host python nudge.
    (sub / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (sub / "b.ts").write_text("export const b = 2\n", encoding="utf-8")
    (sub / "c.js").write_text("console.log(1)\n", encoding="utf-8")
    (sub / "d.js").write_text("console.log(2)\n", encoding="utf-8")

    # .gitmodules declares the submodule path (the host repo's manifest of
    # which subtrees are foreign).
    (host / ".gitmodules").write_text(
        '[submodule "vendor/sub"]\n\tpath = vendor/sub\n\turl = ../sub\n',
        encoding="utf-8",
    )
    return host


def test_g7_detect_submodule_does_not_pollute_host_language_walk(
    tmp_path: Path,
) -> None:
    """Non-git (walk-fallback) host tree: the submodule's nodejs working tree
    must NOT flip the HOST python detection."""
    from qa.detect import detect_language

    host = _build_host_python_with_nodejs_submodule(tmp_path)
    # No .git → forces the os.walk fallback path of iter_repo_files, which is
    # the path that descends into the submodule working tree.
    assert not (host / ".git").exists()

    lang = detect_language(host)
    assert lang == "python", (
        f"host repo declares python (pyproject + .py); submodule vendor/sub is "
        f"declared in .gitmodules and must be excluded from the weighted scan, "
        f"but detect_language returned {lang!r} (polluted by the submodule)"
    )


def test_g7_detect_submodule_parsed_from_gitmodules_is_the_mechanism(
    tmp_path: Path,
) -> None:
    """The exclude is driven by parsing ``.gitmodules``: a submodule whose
    nodejs working tree would otherwise flip detection is excluded ONLY because
    ``.gitmodules`` declares its path. (Broken-control: deleting the
    ``.gitmodules`` declaration re-pollutes → nodejs.)"""
    from qa.detect import detect_language

    host = _build_host_python_with_nodejs_submodule(tmp_path)
    assert (host / ".gitmodules").exists()
    assert detect_language(host) == "python"

    # Remove the declaration → vendor/sub is now an ordinary subdir and must
    # legitimately pollute (proves .gitmodules is the load-bearing mechanism,
    # not a blanket "vendor/" skip).
    (host / ".gitmodules").unlink()
    assert detect_language(host) == "nodejs", (
        "without a .gitmodules declaration the subtree is ordinary source and "
        "must be counted — proves the exclude keys off .gitmodules, not a name"
    )


def test_g7_detect_git_fastpath_unaffected_for_plain_python_repo(
    tmp_path: Path,
) -> None:
    """Regression guard: a plain git python repo (no submodules) still detects
    python — the .gitmodules parsing must be a no-op when absent."""
    from qa.detect import detect_language

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    _init_git_repo(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    assert detect_language(repo) == "python"
