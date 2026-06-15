"""Specialist-role dispatch completeness + legacy-config backfill (Cluster C1).

The Run-4 DEAD-ON-ARRIVAL bug: the intake/diagnosis (and resolver) specialist
roles are NOT in ``REQUIRED_AGENT_ROLES``, so ``require_all_roles`` never
validated them. An on-disk ``config.json`` written before a given specialist
existed therefore lacked its ``cfg.agents[role]`` entry; the self-contained
phase dispatch (``orch.cfg.agents[role]``) then raised ``KeyError`` → silent
fail-safe degrade (intake/diagnosis were no-ops every run).

The ROOT fix lives in ``config.loader._backfill_specialist_roles`` (run at load
time, before ``require_all_roles``). These tests FORMALIZE that fix as a
regression guard at two altitudes:

  a. a real Orchestrator built from ``default_config()`` can read
     ``cfg.agents[role]`` for EVERY specialist role without ``KeyError``;
  b. a legacy ``config.json`` missing the specialist roles + the
     resolver/intake/diagnosis blocks, loaded via ``config.loader.load_config``,
     comes back with every ``SPECIALIST_ROLES`` entry present and the resolver
     enabled.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents import build_registry
from config.defaults import default_config
from config.loader import load_config
from config.schema import SPECIALIST_ROLES
from orchestrator import Orchestrator
from stub_adapter import StubAdapter


def _make_orch(cwd: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="sess-dispatch",
    )


# --------------------------------------------------------------------------- #
# a. Registry/dispatch completeness on a fresh default_config()
# --------------------------------------------------------------------------- #


def test_default_config_has_every_specialist_role() -> None:
    """A fresh default config carries a cfg.agents entry for every specialist."""
    cfg = default_config()
    for role in SPECIALIST_ROLES:
        assert role in cfg.agents, f"missing specialist role {role!r} in default_config"


def test_orchestrator_can_dispatch_every_specialist_role(tmp_path: Path) -> None:
    """A real Orchestrator from default_config() can read cfg.agents[role] for
    every specialist (framing, altitude_judge, intake_enricher, intake_clarifier,
    diagnostician, resolver) WITHOUT KeyError — the exact lookup every
    self-contained phase dispatch performs."""
    orch = _make_orch(tmp_path)
    expected = {
        "framing",
        "altitude_judge",
        "intake_enricher",
        "intake_clarifier",
        "diagnostician",
        "resolver",
    }
    # Every role we expect must be in SPECIALIST_ROLES (catch drift).
    assert expected <= set(SPECIALIST_ROLES)
    for role in SPECIALIST_ROLES:
        # This is the access pattern that raised KeyError in Run-4.
        agent_cfg = orch.cfg.agents[role]
        assert agent_cfg is not None
        # max_turns must be a sane dispatchable budget (>=1) for every specialist.
        assert (agent_cfg.max_turns or 1) >= 1


# --------------------------------------------------------------------------- #
# b. Legacy-config backfill via config.loader.load_config
# --------------------------------------------------------------------------- #


def _legacy_config_dict() -> dict:
    """A minimal-but-valid on-disk config that PREDATES the specialist roles +
    the resolver/intake/diagnosis blocks (mirrors a pre-v0.41 config.json).

    Starts from a fresh default config dump, then strips every specialist role
    out of ``agents`` and removes the resolver/intake/diagnosis top-level blocks
    — exactly the shape that triggered the Run-4 KeyError-at-dispatch.
    """
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    # Strip the specialist roles a legacy config would not have written.
    for role in SPECIALIST_ROLES:
        data["agents"].pop(role, None)
    # Strip the new top-level phase blocks (legacy default_factory fills them).
    for block in ("resolver", "intake", "diagnosis"):
        data.pop(block, None)
    return data


def test_legacy_config_backfills_specialist_roles(tmp_path: Path) -> None:
    """A legacy config missing every specialist role round-trips through
    load_config with ALL SPECIALIST_ROLES present (the loader backfill)."""
    data = _legacy_config_dict()
    # Sanity: the on-disk form genuinely lacks the specialist roles.
    for role in SPECIALIST_ROLES:
        assert role not in data["agents"]

    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    cfg = load_config(path)
    for role in SPECIALIST_ROLES:
        assert role in cfg.agents, f"loader did not backfill {role!r}"
        assert cfg.agents[role].model, f"backfilled {role!r} has no model"


def test_legacy_config_backfills_resolver_enabled(tmp_path: Path) -> None:
    """The resolver block (absent in a legacy config) is restored on by default."""
    data = _legacy_config_dict()
    assert "resolver" not in data

    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.resolver.enabled is True


def test_loader_backfill_is_idempotent_and_nondestructive(tmp_path: Path) -> None:
    """A config that ALREADY carries a customized specialist role is not
    overwritten by the backfill (operator customization survives)."""
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    # Operator pinned a custom model + turn budget for the diagnostician.
    data["agents"]["diagnostician"]["model"] = "custom-operator-model"
    data["agents"]["diagnostician"]["max_turns"] = 9

    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    loaded = load_config(path)
    assert loaded.agents["diagnostician"].model == "custom-operator-model"
    assert loaded.agents["diagnostician"].max_turns == 9
