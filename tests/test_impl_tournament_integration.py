"""Integration tests for :class:`ImplTournament` with stubbed adapter + CoderRunner."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tournament import (
    ImplBundle,
    ImplContentHandler,
    ImplTournament,
    StubLLMClient,
    TournamentConfig,
)


INITIAL_DIFF = "+def foo(): return 1\n"
INITIAL_BUNDLE = ImplBundle(
    task_id="1.1",
    task_description="Add foo()",
    diff=INITIAL_DIFF,
    files_changed=["foo.py"],
    tests_passed=3,
    tests_failed=0,
    tests_total=3,
    test_output_excerpt="3 passed",
)

B_DIFF = "+def foo(): return 2  # improved\n"
AB_DIFF = "+def foo(): return 3  # synthesized\n"


class _StubCoderRunner:
    def __init__(self, b_diff: str = B_DIFF, ab_diff: str = AB_DIFF) -> None:
        self._b_diff = b_diff
        self._ab_diff = ab_diff
        self.calls: list[tuple[str, str]] = []

    async def run(
        self,
        variant_label: str,
        direction: str,
        worktree: Path,
        task: ImplBundle,
    ) -> ImplBundle:
        self.calls.append((variant_label, direction))
        diff = self._b_diff if variant_label == "B" else self._ab_diff
        return ImplBundle(
            task_id=task.task_id,
            task_description=task.task_description,
            diff=diff,
            files_changed=task.files_changed,
            tests_passed=3,
            tests_failed=0,
            tests_total=3,
            test_output_excerpt="3 passed",
            variant_label=variant_label,  # type: ignore[arg-type]
        )


class _NoopWorktreeManager:
    async def create(self, label: str, base_ref: str = "HEAD") -> Path:
        return Path("/tmp/fake-worktree") / label

    async def cleanup_all(self) -> None:
        pass


def _always_a_cb(role: str, system: str, user: str) -> str:
    if role == "critic_t":
        return "Critic: no issues."
    if role == "architect_b":
        return "- No changes needed"
    if role == "synthesizer":
        return "- Keep as is"
    if role == "judge":
        # Equal scores -> conservative tiebreak picks A.
        return "All equal.\n\nRANKING: 1, 2, 3"
    return "default"


def _always_slot2_cb(role: str, system: str, user: str) -> str:
    if role == "critic_t":
        return "Critic: issues."
    if role == "architect_b":
        return "- Fix it"
    if role == "synthesizer":
        return "- Synthesize"
    if role == "judge":
        return "Evaluation.\n\nRANKING: 2, 1, 3"
    return "default"


@pytest.mark.asyncio
async def test_impl_tournament_a_wins_converges(tmp_path: Path) -> None:
    """When judge always ties (equal scores), conservative tiebreak picks A."""
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=5)
    client = StubLLMClient(fn=_always_a_cb)
    runner = _StubCoderRunner()

    tour = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path / "t-a-wins",
        rng=random.Random(0),
        coder_runner=runner,
        worktree_manager=_NoopWorktreeManager(),
    )
    final, history = await tour.run(task_prompt="Add foo()", initial=INITIAL_BUNDLE)

    # With equal scores and conservative tiebreak, A eventually wins.
    # The exact number of passes depends on RNG shuffle order.
    assert len(history) <= 5
    # The last passes should be A wins (convergence).
    assert history[-1].winner == "A"
    assert history[-2].winner == "A"


@pytest.mark.asyncio
async def test_impl_tournament_max_rounds_respected(tmp_path: Path) -> None:
    """Tournament stops at max_rounds even without convergence."""
    cfg = TournamentConfig(num_judges=1, convergence_k=3, max_rounds=2)
    client = StubLLMClient(fn=_always_slot2_cb)
    runner = _StubCoderRunner()

    tour = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path / "t-max-rounds",
        rng=random.Random(0),
        coder_runner=runner,
        worktree_manager=_NoopWorktreeManager(),
    )
    final, history = await tour.run(task_prompt="Add foo()", initial=INITIAL_BUNDLE)

    assert len(history) == 2


@pytest.mark.asyncio
async def test_impl_tournament_coder_runner_called_per_pass(tmp_path: Path) -> None:
    """CoderRunner is called for B and AB variants each pass."""
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    client = StubLLMClient(fn=_always_a_cb)
    runner = _StubCoderRunner()

    tour = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path / "t-runner-calls",
        rng=random.Random(0),
        coder_runner=runner,
        worktree_manager=_NoopWorktreeManager(),
    )
    await tour.run(task_prompt="Add foo()", initial=INITIAL_BUNDLE)

    labels = [c[0] for c in runner.calls]
    assert "B" in labels
    assert "AB" in labels


@pytest.mark.asyncio
async def test_impl_tournament_artifacts_written(tmp_path: Path) -> None:
    """Tournament writes initial_a.md, final_output.md, history.json."""
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    client = StubLLMClient(fn=_always_a_cb)
    runner = _StubCoderRunner()
    artifact_dir = tmp_path / "artifacts"

    tour = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact_dir,
        rng=random.Random(0),
        coder_runner=runner,
        worktree_manager=_NoopWorktreeManager(),
    )
    await tour.run(task_prompt="Add foo()", initial=INITIAL_BUNDLE)

    assert (artifact_dir / "initial_a.md").exists()
    assert (artifact_dir / "final_output.md").exists()
    assert (artifact_dir / "history.json").exists()
    assert (artifact_dir / "pass_01").is_dir()
