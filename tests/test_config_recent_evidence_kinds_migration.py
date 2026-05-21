"""v0.38.0 HK1: ``recent_evidence_include_kinds`` legacy ``"coder"`` → ``"developer"`` shim.

The user-facing label diverged from the on-disk
:class:`state.schemas.CoderEvidence.kind` discriminator (``"developer"``)
for one release. v0.38.0 unifies the names: configs carrying the legacy
``"coder"`` string are rewritten to ``"developer"`` by a
``@model_validator(mode="after")`` shim that fires a one-shot
``config.deprecated_kind_label`` warning. Scheduled for hard-removal in
v0.39.0.

Covered scenarios:
  1. Default config carries the new ``"developer"`` label directly.
  2. Legacy ``"coder"`` in include_kinds is rewritten to ``"developer"``
     AND a DeprecationWarning fires.
  3. Mixed legacy + new labels rewrite the legacy ones, preserve the rest.
  4. The shim is order-preserving (config order matters for prompt rendering).
  5. Multiple legacy entries trigger the warning ONCE per process
     (re-entrant guard).
  6. Empty include_kinds → no rewrite, no warning.
"""

from __future__ import annotations

import warnings

import pytest

from config.schema import AutodevConfig, _warned_kind_labels
from config.defaults import default_config


@pytest.fixture(autouse=True)
def _reset_warned_kinds() -> None:
    """The one-shot warning ledger is process-global by design. Reset
    it between tests so deprecation-warning assertions are deterministic."""
    _warned_kind_labels.clear()


def test_default_config_uses_developer_label() -> None:
    """v0.38.0 HK1: the schema default is now ``["review", "test", "developer"]``,
    no legacy ``"coder"`` entry."""
    cfg = default_config()
    assert "coder" not in cfg.recent_evidence_include_kinds
    assert "developer" in cfg.recent_evidence_include_kinds


def test_legacy_coder_rewritten_to_developer() -> None:
    """v0.38.0 HK1: a config carrying the legacy ``"coder"`` string is
    rewritten in-place to ``"developer"`` and emits a deprecation
    warning."""
    base = default_config().model_dump(mode="json")
    base["recent_evidence_include_kinds"] = ["review", "test", "coder"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = AutodevConfig.model_validate(base)

    assert cfg.recent_evidence_include_kinds == ["review", "test", "developer"]
    # Exactly one DeprecationWarning fires per process (re-entrant guard).
    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert len(dep_warnings) >= 1, (
        "HK1: legacy 'coder' should trigger a DeprecationWarning"
    )
    assert "coder" in str(dep_warnings[0].message)
    assert "developer" in str(dep_warnings[0].message)
    assert "v0.39.0" in str(dep_warnings[0].message)


def test_legacy_coder_order_preserved() -> None:
    """v0.38.0 HK1: the rewrite preserves config order — order matters
    for the prompt-rendering pipeline."""
    base = default_config().model_dump(mode="json")
    base["recent_evidence_include_kinds"] = ["coder", "review", "test"]
    cfg = AutodevConfig.model_validate(base)
    assert cfg.recent_evidence_include_kinds == ["review", "test", "developer"] or \
        cfg.recent_evidence_include_kinds == ["developer", "review", "test"]
    # Specifically: developer should land in the position the legacy
    # "coder" occupied.
    assert cfg.recent_evidence_include_kinds[0] == "developer"


def test_legacy_warning_fires_once_per_process() -> None:
    """v0.38.0 HK1: the re-entrant guard prevents log spam when many
    configs load in a long-running fleet."""
    base = default_config().model_dump(mode="json")
    base["recent_evidence_include_kinds"] = ["coder"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        AutodevConfig.model_validate(base)
        AutodevConfig.model_validate(base)
        AutodevConfig.model_validate(base)

    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    # Exactly one warning across the three validate calls.
    assert len(dep_warnings) == 1, (
        f"HK1: warning should fire once per process, got {len(dep_warnings)}"
    )


def test_no_legacy_no_warning_no_rewrite() -> None:
    """v0.38.0 HK1: a clean config (already on 'developer') triggers
    neither the warning nor a rewrite — no false-positive churn."""
    base = default_config().model_dump(mode="json")
    base["recent_evidence_include_kinds"] = ["review", "test", "developer"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = AutodevConfig.model_validate(base)
    assert cfg.recent_evidence_include_kinds == [
        "review",
        "test",
        "developer",
    ]
    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert dep_warnings == []


def test_empty_kinds_no_warning() -> None:
    """v0.38.0 HK1: empty list (operator escape hatch for legacy
    one-liner behaviour) does not trigger the shim."""
    base = default_config().model_dump(mode="json")
    base["recent_evidence_include_kinds"] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = AutodevConfig.model_validate(base)
    assert cfg.recent_evidence_include_kinds == []
    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert dep_warnings == []
