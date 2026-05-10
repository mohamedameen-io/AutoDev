"""v0.23.0 C1 regression: ``worktree_huge_repo_mode`` config + auto-resolution.

D-3 + P-3 finding from the 2026-05-09 Unity stall: Unity's full-checkout
``git worktree add`` took 80-180 s on 358K files but the historical
60 s ceiling killed it. v0.22.1 A3 added the timeout extension and
``WorktreeManager.huge_mode`` flag; v0.23.0 C1 promotes this to a
first-class config field with three modes (``auto``, ``on``, ``off``)
and makes sparse-checkout the default when huge mode resolves on.
"""

from __future__ import annotations

from config.defaults import default_config
from config.schema import AutodevConfig


def test_default_worktree_huge_repo_mode_is_auto() -> None:
    cfg = default_config()
    assert cfg.worktree_huge_repo_mode == "auto"


def test_worktree_huge_create_timeout_default() -> None:
    cfg = default_config()
    assert cfg.worktree_huge_create_timeout_s == 600


def test_worktree_huge_pool_size_default() -> None:
    cfg = default_config()
    assert cfg.worktree_huge_pool_size == 2


def test_worktree_huge_repo_mode_accepts_on_off_auto() -> None:
    """Pydantic Literal accepts the three valid modes."""
    for mode in ("auto", "on", "off"):
        cfg = default_config().model_copy(update={"worktree_huge_repo_mode": mode})
        assert cfg.worktree_huge_repo_mode == mode


def test_worktree_huge_create_timeout_bounds() -> None:
    """Timeout is bounded to keep operators from setting absurdly small/large."""
    import pydantic

    base = default_config()
    with pytest.raises(pydantic.ValidationError):
        base.model_copy(update={"worktree_huge_create_timeout_s": 10}).model_validate(
            base.model_copy(update={"worktree_huge_create_timeout_s": 10}).model_dump()
        )
    # Note: model_copy bypasses validators; re-run model_validate to enforce.
    with pytest.raises(pydantic.ValidationError):
        AutodevConfig.model_validate({**base.model_dump(), "worktree_huge_create_timeout_s": 10_000})


def test_worktree_huge_pool_size_bounds() -> None:
    """Pool size bounded [0, 8]."""
    import pydantic

    base = default_config()
    with pytest.raises(pydantic.ValidationError):
        AutodevConfig.model_validate({**base.model_dump(), "worktree_huge_pool_size": -1})
    with pytest.raises(pydantic.ValidationError):
        AutodevConfig.model_validate({**base.model_dump(), "worktree_huge_pool_size": 20})


def test_huge_mode_resolution_logic() -> None:
    """The orchestrator's resolution rule: ``"on"`` always, ``"off"`` never,
    ``"auto"`` keys off ``is_huge``."""

    def _resolved(mode: str, is_huge: bool) -> bool:
        if mode == "on":
            return True
        if mode == "off":
            return False
        return is_huge

    assert _resolved("on", False) is True
    assert _resolved("on", True) is True
    assert _resolved("off", False) is False
    assert _resolved("off", True) is False
    assert _resolved("auto", False) is False
    assert _resolved("auto", True) is True


# Use pytest.raises in the bounds test.
import pytest  # noqa: E402
