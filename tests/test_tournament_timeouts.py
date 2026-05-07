"""Tests for :mod:`tournament.timeouts` — per-role timeout resolution.

Mirrors ``tournament.effort`` in shape: a static table keyed by
``role -> complexity -> seconds`` with a single resolver helper.
"""

from __future__ import annotations

import pytest

from tournament.timeouts import ROLE_TIMEOUT_S, resolve_role_timeout_s


# ── Table shape ────────────────────────────────────────────────────────────


def test_role_timeout_table_has_expected_roles() -> None:
    """The four tournament roles must each have a complexity-keyed dict."""
    assert "architect_b" in ROLE_TIMEOUT_S
    assert "synthesizer" in ROLE_TIMEOUT_S
    assert "critic_t" in ROLE_TIMEOUT_S
    assert "judge" in ROLE_TIMEOUT_S


def test_role_timeout_table_has_three_complexities_per_role() -> None:
    """Each role must have entries for simple/medium/complex."""
    for role, by_complexity in ROLE_TIMEOUT_S.items():
        assert set(by_complexity.keys()) == {"simple", "medium", "complex"}, (
            f"{role} missing one of simple/medium/complex"
        )


# ── Resolver: golden values ────────────────────────────────────────────────


def test_resolve_role_timeout_s_complex_architect_b_returns_1200() -> None:
    """architect_b gets 1200s on complex plans (the QNX failure case)."""
    assert resolve_role_timeout_s("architect_b", "complex") == 1200


def test_resolve_role_timeout_s_medium_architect_b_returns_600() -> None:
    """architect_b stays at 600s on medium plans (default)."""
    assert resolve_role_timeout_s("architect_b", "medium") == 600


def test_resolve_role_timeout_s_simple_architect_b_returns_600() -> None:
    """architect_b stays at 600s on simple plans (default)."""
    assert resolve_role_timeout_s("architect_b", "simple") == 600


def test_resolve_role_timeout_s_complex_synthesizer_returns_900() -> None:
    """synthesizer gets 900s on complex plans."""
    assert resolve_role_timeout_s("synthesizer", "complex") == 900


def test_resolve_role_timeout_s_complex_critic_t_returns_600() -> None:
    """critic_t gets 600s on complex plans (less than authors)."""
    assert resolve_role_timeout_s("critic_t", "complex") == 600


def test_resolve_role_timeout_s_complex_judge_returns_300() -> None:
    """judge stays at 300s — short ranking task even on complex plans."""
    assert resolve_role_timeout_s("judge", "complex") == 300


def test_resolve_role_timeout_s_medium_critic_t_returns_300() -> None:
    """critic_t stays at 300s on medium plans."""
    assert resolve_role_timeout_s("critic_t", "medium") == 300


# ── Resolver: missing keys → None ──────────────────────────────────────────


def test_resolve_role_timeout_s_unknown_role_returns_none() -> None:
    """An unknown role returns None (caller falls back to default)."""
    assert resolve_role_timeout_s("explorer", "complex") is None


def test_resolve_role_timeout_s_none_complexity_returns_none() -> None:
    """A None complexity returns None (no plan classification yet)."""
    assert resolve_role_timeout_s("architect_b", None) is None


def test_resolve_role_timeout_s_unknown_complexity_returns_none() -> None:
    """A complexity outside simple/medium/complex returns None."""
    assert resolve_role_timeout_s("architect_b", "exotic") is None


# ── Parametrized cross-check vs. table ─────────────────────────────────────


@pytest.mark.parametrize(
    "role,complexity",
    [
        ("architect_b", "simple"),
        ("architect_b", "medium"),
        ("architect_b", "complex"),
        ("synthesizer", "simple"),
        ("synthesizer", "medium"),
        ("synthesizer", "complex"),
        ("critic_t", "simple"),
        ("critic_t", "medium"),
        ("critic_t", "complex"),
        ("judge", "simple"),
        ("judge", "medium"),
        ("judge", "complex"),
    ],
)
def test_resolver_matches_table(role: str, complexity: str) -> None:
    """The resolver returns exactly the table value for every legal pair."""
    assert resolve_role_timeout_s(role, complexity) == ROLE_TIMEOUT_S[role][complexity]
