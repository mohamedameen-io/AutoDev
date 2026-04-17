"""Load and save `.autodev/config.json`."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from config.schema import AutodevConfig
from errors import ConfigError


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
