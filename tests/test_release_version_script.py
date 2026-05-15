"""Tests for ``scripts/release_version.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import importlib.util


def _load_module():
    repo_root = Path(__file__).resolve().parent.parent
    spec_path = repo_root / "scripts" / "release_version.py"
    spec = importlib.util.spec_from_file_location("release_version", spec_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release_version = _load_module()


# --- helpers -----------------------------------------------------------------


def _scaffold(tmp_path: Path, *, version: str, changelog_version: str | None,
              npm_version: str = "0.24.0") -> dict[str, Path]:
    vfile = tmp_path / "_version.py"
    vfile.write_text(f'__version__ = "{version}"\n')
    cfile = tmp_path / "CHANGELOG.md"
    if changelog_version is not None:
        cfile.write_text(
            "# Changelog\n\n"
            f"## [{changelog_version}] - 2026-05-15\n\n- entry\n"
        )
    else:
        cfile.write_text("# Changelog\n")
    nfile = tmp_path / "package.json"
    nfile.write_text(json.dumps({"name": "ai-autodev", "version": npm_version}))
    return {"version": vfile, "changelog": cfile, "npm": nfile}


# --- tests -------------------------------------------------------------------


def test_rejects_invalid_format() -> None:
    with pytest.raises(release_version.ValidationError):
        release_version.validate_format("v1.0.0")
    with pytest.raises(release_version.ValidationError):
        release_version.validate_format("1.0")
    with pytest.raises(release_version.ValidationError):
        release_version.validate_format("1.0.0-rc1")
    # valid
    release_version.validate_format("0.31.0")


def test_rejects_missing_changelog(tmp_path: Path) -> None:
    files = _scaffold(tmp_path, version="0.30.2", changelog_version=None)
    with pytest.raises(release_version.ValidationError, match="missing"):
        release_version.validate_changelog("0.31.0", path=files["changelog"])


def test_succeeds_with_valid_inputs(tmp_path: Path) -> None:
    files = _scaffold(
        tmp_path,
        version="0.30.2",
        changelog_version="0.31.0",
        npm_version="0.31.0",
    )
    release_version.validate_format("0.31.0")
    release_version.validate_changelog("0.31.0", path=files["changelog"])
    warning = release_version.validate_npm(
        "0.31.0", allow_mismatch=False, path=files["npm"]
    )
    assert warning is None
    release_version.write_version("0.31.0", path=files["version"])
    assert '__version__ = "0.31.0"' in files["version"].read_text()


def test_npm_mismatch_warns_unless_allowed(tmp_path: Path) -> None:
    files = _scaffold(
        tmp_path,
        version="0.30.2",
        changelog_version="0.31.0",
        npm_version="0.24.0",
    )
    # Without --allow-npm-mismatch: hard fail.
    with pytest.raises(release_version.ValidationError, match="0.24.0"):
        release_version.validate_npm(
            "0.31.0", allow_mismatch=False, path=files["npm"]
        )
    # With it: returns a warning string but does not raise.
    warning = release_version.validate_npm(
        "0.31.0", allow_mismatch=True, path=files["npm"]
    )
    assert warning is not None
    assert "0.24.0" in warning
    assert "allowed" in warning.lower()
