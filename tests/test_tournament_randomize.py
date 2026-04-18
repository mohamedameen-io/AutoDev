"""Tests for randomize_for_judge — deterministic shuffling + order_map inverse."""

from __future__ import annotations

import random
from collections import Counter

from tournament import randomize_for_judge


_A = "VERSION_A_TEXT"
_B = "VERSION_B_TEXT"
_AB = "VERSION_AB_TEXT"


def test_seeded_rng_is_deterministic() -> None:
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    order1 = randomize_for_judge(_A, _B, _AB, rng1)
    order2 = randomize_for_judge(_A, _B, _AB, rng2)
    assert order1 == order2


def test_all_permutations_reachable() -> None:
    """Across many seeds, all 6 permutations of (A, B, AB) should appear."""
    seen: set[tuple[str, str, str]] = set()
    for seed in range(200):
        rng = random.Random(seed)
        order = randomize_for_judge(_A, _B, _AB, rng)
        seen.add((order[1], order[2], order[3]))
    # 3! = 6 permutations exist; with 200 seeds we should see every one.
    assert len(seen) == 6


def test_order_map_is_valid_permutation() -> None:
    for seed in range(20):
        rng = random.Random(seed)
        order = randomize_for_judge(_A, _B, _AB, rng)
        assert set(order.keys()) == {1, 2, 3}
        assert set(order.values()) == {"A", "B", "AB"}


def test_proposals_match_order_map() -> None:
    """order_map must be a bijection from {1,2,3} to {"A","B","AB"}."""
    for seed in range(20):
        rng = random.Random(seed)
        order = randomize_for_judge(_A, _B, _AB, rng)
        assert set(order.keys()) == {1, 2, 3}
        assert set(order.values()) == {"A", "B", "AB"}


def test_distribution_of_first_position_is_uniform() -> None:
    """Sanity: over 600 seeds, each label appears as position-1 ≈ 200 times."""
    counts: Counter[str] = Counter()
    for seed in range(600):
        rng = random.Random(seed)
        order = randomize_for_judge(_A, _B, _AB, rng)
        counts[order[1]] += 1
    # Each label should appear in position 1 roughly 1/3 of the time.
    # Allow wide tolerance to keep the test flake-proof.
    for label in ("A", "B", "AB"):
        assert 130 < counts[label] < 270


def test_inverse_mapping_via_order() -> None:
    """`order` can map a judge-emitted position back to the canonical label."""
    rng = random.Random(7)
    order = randomize_for_judge(_A, _B, _AB, rng)
    # Imagine judge returned RANKING: 1, 3, 2 — inverse-map each digit.
    raw_ranking = [1, 3, 2]
    mapped = [order[d] for d in raw_ranking]
    assert set(mapped) == {"A", "B", "AB"}
