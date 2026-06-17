"""WS2-20: JS package-manager resolution for the QA gates.

The JS gates (eslint / tsc / npm build / npm test) historically hardcoded
``npx`` / ``npm``, so yarn/pnpm workspaces and project-local
``node_modules/.bin`` installs were ignored. :mod:`qa.env` now exposes
``detect_js_package_manager`` and ``resolve_js_tool`` to resolve the right
manager and prefer the project-pinned binary.

Engagement proof:
  * RED-on-HEAD: ``resolve_js_tool`` / ``detect_js_package_manager`` did not
    exist before this change — the import below would ImportError, so every
    test here was uncollectable (red) on HEAD.
  * BROKEN-CONTROL: ``test_pnpm_is_not_npm`` and ``test_yarn_is_not_npm`` fail
    if the resolver reverts to "npm always" (the old hardcoded behavior).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.env import detect_js_package_manager, resolve_js_tool


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# detect_js_package_manager — lockfile-driven detection
# ---------------------------------------------------------------------------


def test_detect_pnpm_from_lockfile(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert detect_js_package_manager(tmp_path) == "pnpm"


def test_detect_yarn_from_lockfile(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    assert detect_js_package_manager(tmp_path) == "yarn"


def test_detect_npm_from_lockfile(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert detect_js_package_manager(tmp_path) == "npm"


def test_detect_npm_default_no_lockfile(tmp_path: Path) -> None:
    # No lockfile at all → npm baseline (always available with Node).
    assert detect_js_package_manager(tmp_path) == "npm"


def test_detect_pnpm_beats_yarn_and_npm(tmp_path: Path) -> None:
    # Mixed lockfiles (a migrated repo) → pnpm wins per the cascade.
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert detect_js_package_manager(tmp_path) == "pnpm"


# ---------------------------------------------------------------------------
# resolve_js_tool — node_modules/.bin precedence + manager fallback
# ---------------------------------------------------------------------------


def test_local_bin_beats_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project-local ``node_modules/.bin/<tool>`` is used before any manager."""
    local_bin = tmp_path / "node_modules" / ".bin" / "eslint"
    _touch(local_bin)
    # Even with a pnpm lockfile + pnpm on PATH, the pinned binary wins.
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/pnpm")
    assert resolve_js_tool(tmp_path, "eslint") == [str(local_bin)]


def test_pnpm_exec_when_lockfile_and_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None)
    assert resolve_js_tool(tmp_path, "tsc") == ["pnpm", "exec", "tsc"]


def test_yarn_exec_when_lockfile_and_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda name: "/usr/bin/yarn" if name == "yarn" else None)
    assert resolve_js_tool(tmp_path, "eslint") == ["yarn", "exec", "eslint"]


def test_npm_exec_for_package_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    assert resolve_js_tool(tmp_path, "tsc") == ["npm", "exec", "tsc"]


def test_npx_fallback_when_manager_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pnpm named by lockfile but NOT on PATH → degrade to ``npx --no-install``."""
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda name: "/usr/bin/npx" if name == "npx" else None)
    assert resolve_js_tool(tmp_path, "eslint") == ["npx", "--no-install", "eslint"]


def test_bare_tool_when_nothing_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: None)
    assert resolve_js_tool(tmp_path, "eslint") == ["eslint"]


# ---------------------------------------------------------------------------
# Broken-control: revert to "npm always" must turn these red.
# ---------------------------------------------------------------------------


def test_pnpm_is_not_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of WS2-20: a pnpm repo must NOT resolve to npm."""
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/x")
    resolved = resolve_js_tool(tmp_path, "tsc")
    assert "npm" not in resolved
    assert resolved[0] == "pnpm"


def test_yarn_is_not_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/x")
    resolved = resolve_js_tool(tmp_path, "eslint")
    assert "npm" not in resolved
    assert resolved[0] == "yarn"
