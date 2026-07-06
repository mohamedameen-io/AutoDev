"""Phase-1 scaffolding gate: new sub-packages import, and pyproject declares
the markers + optional-dependency group the rest of Phase 1 relies on.

Non-vacuous: each assertion targets something absent before P1.0 lands
(the sub-packages don't exist; the markers/group aren't registered) and
present after, so a regression that removes any of them fails this test.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 fallback, unused here
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module_name",
    [
        "benchmarks.adapters",
        "benchmarks.scorers",
        "benchmarks.datasets",
        "benchmarks.gate",
    ],
)
def test_new_subpackages_import(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__doc__, f"{module_name} must carry a module docstring"


def _load_pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_pytest_markers_registered() -> None:
    data = _load_pyproject()
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    marker_names = {m.split(":", 1)[0].strip() for m in markers}
    assert "swebench" in marker_names
    assert "bench_external" in marker_names
    # existing marker must survive the append
    assert "resolver_enabled" in marker_names


def test_swebench_optional_dependency_group() -> None:
    data = _load_pyproject()
    optional_deps = data["project"]["optional-dependencies"]
    assert "swebench" in optional_deps
    group = optional_deps["swebench"]
    assert any(dep.split(">=")[0].split("==")[0].strip() == "sb-cli" for dep in group)
    # keep it minimal: datasets/huggingface_hub must NOT be forced in via this
    # group (the dataset loader lazy-imports / degrades instead)
    joined = " ".join(group).lower()
    assert "datasets" not in joined
    assert "huggingface_hub" not in joined
    # must not have been folded into dev
    dev_joined = " ".join(data["project"]["optional-dependencies"].get("dev", [])).lower()
    assert "sb-cli" not in dev_joined
