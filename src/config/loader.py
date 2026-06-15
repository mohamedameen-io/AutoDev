"""Load and save `.autodev/config.json`."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from config.schema import SPECIALIST_ROLES, AgentConfig, AutodevConfig
from errors import ConfigError


def _backfill_specialist_roles(cfg: AutodevConfig) -> None:
    """v0.42.0 (C1): add a default :class:`AgentConfig` for any specialist role
    missing from an on-disk config, in place. Idempotent.

    The Run-4 DEAD-ON-ARRIVAL bug: specialist roles (``framing``,
    ``intake_enricher``, ``diagnostician``, ``resolver`` …) are *not* in
    :data:`REQUIRED_AGENT_ROLES`, so :meth:`AutodevConfig.require_all_roles`
    never validated them. A ``.autodev/config.json`` written by a build that
    predates a given specialist role therefore lacks its ``cfg.agents[role]``
    entry — and the self-contained phase dispatch (``cfg.agents[role]``) then
    raises ``KeyError`` → silent fail-safe degrade (intake/diagnosis were no-ops
    every run). A *fresh* config is unaffected because
    :func:`config.defaults.default_config` builds ``agents`` from the full
    ``_AGENT_MODEL_DEFAULTS`` map; only on-disk legacy configs hit the gap.

    Backfill is **idempotent and non-destructive**: a role already present
    (e.g. an operator customization) is never overwritten. The model/turns
    mirror :func:`config.defaults.default_config` so a backfilled legacy config
    is byte-identical to a fresh config for that role.
    """
    # Imported lazily to avoid a config.loader -> config.defaults import at
    # module load (defaults imports schema; loader stays leaf-light).
    from config.defaults import _AGENT_MAX_TURNS, resolve_model

    for role in SPECIALIST_ROLES:
        if role in cfg.agents:
            continue
        cfg.agents[role] = AgentConfig(
            model=resolve_model(None, role, cfg.platform),
            max_turns=_AGENT_MAX_TURNS.get(role, 1),
        )


def load_config(path: Path) -> AutodevConfig:
    """Load and validate a config file. Raises ConfigError on any failure."""
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    try:
        cfg = AutodevConfig.model_validate_json(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"malformed JSON at {path}: {exc}") from exc
    # v0.42.0 (C1): backfill specialist roles BEFORE require_all_roles so a
    # legacy config never KeyErrors at specialist dispatch. require_all_roles
    # still guards the 14 REQUIRED_AGENT_ROLES — a genuinely broken config
    # (missing a core role) still fails loudly here, not silently at dispatch.
    _backfill_specialist_roles(cfg)
    try:
        cfg.require_all_roles()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return cfg


def save_config(cfg: AutodevConfig, path: Path) -> None:
    """Write config as JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def expand_paths(cfg: AutodevConfig) -> AutodevConfig:
    """Return a copy with user-home paths resolved (currently just hive.path)."""
    expanded = cfg.model_copy(deep=True)
    expanded.hive.path = Path(expanded.hive.path).expanduser()
    return expanded
