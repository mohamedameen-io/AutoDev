"""Generic self-refinement tournament engine.

Pass loop per round:
    CRITIC -> ARCHITECT_B -> SYNTHESIZER -> N parallel JUDGES -> Borda aggregation.

Parameterized over a `ContentHandler[T]` so the same loop drives plan-markdown
refinement (plan phase) or implementation-bundle refinement (impl phase).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

from errors import TournamentError
from autologging import get_logger
from tournament.prompts import (
    ARCHITECT_B_SYSTEM,
    CRITIC_SYSTEM,
    JUDGE_SYSTEM,
    SYNTHESIZER_SYSTEM,
)
from tournament.state import TournamentArtifactStore

T = TypeVar("T")
WinnerLabel = Literal["A", "B", "AB"]


@runtime_checkable
class LLMClient(Protocol):
    """Minimal async LLM-call interface.

    Phase 2's `PlatformAdapter` satisfies this via `adapter.execute` wrapped in
    `AdapterLLMClient` (see `tournament.llm`).
    """

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str: ...


@runtime_checkable
class ContentHandler(Protocol, Generic[T]):
    """Renders T into role-specific prompt payloads and parses role outputs.

    Phase 6 (PlanTournament) implements this over `T = str` (plan markdown).
    Phase 7 (ImplementationTournament) implements over `T = ImplBundle`.
    """

    def render_for_critic(self, t: T, task_prompt: str) -> str: ...
    def render_for_architect_b(
        self, task_prompt: str, a: T, critic_text: str
    ) -> str: ...
    def render_for_synthesizer(self, task_prompt: str, x: T, y: T) -> str: ...
    def render_for_judge(
        self, task_prompt: str, v_a: T, v_b: T, v_ab: T, order_map: dict[int, str]
    ) -> str:
        """Render judge prompt with proposals in the order dictated by order_map.

        `order_map` maps 1-based display position to canonical label: e.g.
        ``{1: "B", 2: "AB", 3: "A"}`` means proposal 1 shown to the judge is
        variant B, proposal 2 is AB, and proposal 3 is A. Implementations must
        present proposals in this shuffled order so judges cannot infer identity
        from position, and must use the same map to inverse-translate
        judge-emitted position numbers back to canonical labels after judging.
        """
        ...
    def parse_revision(self, revision_text: str, original: T) -> T: ...
    def parse_synthesis(self, synth_text: str, a: T, b: T) -> T: ...
    def render_as_markdown(self, t: T) -> str: ...
    def hash(self, t: T) -> str: ...


@dataclass
class TournamentConfig:
    num_judges: int = 3
    convergence_k: int = 2
    max_rounds: int = 30
    author_temp: float = 0.8  # informational — subscription CLIs don't expose temp
    judge_temp: float = 0.3  # informational — subscription CLIs don't expose temp
    model: str | None = None
    conservative_tiebreak: bool = True
    max_parallel_subprocesses: int = 3


class PassResult(BaseModel):
    pass_num: int
    winner: WinnerLabel
    scores: dict[str, int]
    valid_judges: int
    elapsed_s: float
    judge_details: list[dict[str, Any]] = Field(default_factory=list)
    incumbent_hash_before: str
    incumbent_hash_after: str
    meta: dict[str, Any] = Field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────────────────


def parse_ranking(text: str, valid_labels: str = "123") -> list[str] | None:
    """Parse the last RANKING: line into a list of valid characters.

    Returns a list like `["1","3","2"]` on success or `None` on failure.
    A RANKING with fewer valid digits than `len(valid_labels)` is rejected
    (treated as parse failure) to avoid giving the omitted candidate a
    systematic 0-point disadvantage in Borda aggregation.
    """
    for line in reversed(text.split("\n")):
        line = line.strip().strip("*").strip().lstrip("#").strip()
        if line.upper().startswith("RANKING:"):
            raw = line.split(":", 1)[1].strip()
            items = [c for c in raw if c in valid_labels]
            if len(items) >= len(valid_labels):
                return items
    return None


def randomize_for_judge(
    v_a: T, v_b: T, v_ab: T, rng: random.Random
) -> dict[int, str]:
    """Shuffle (A, B, AB) into a random display order and return the order map.

    Returns `order_map` where `order_map[pos_index]` maps a 1-based display
    index back to the canonical label ("A" | "B" | "AB"). Callers use this to
    pass a consistent order to :meth:`ContentHandler.render_for_judge` and to
    inverse-map judge-emitted position numbers back to canonical labels after
    judging.
    """
    versions = [("A", v_a), ("B", v_b), ("AB", v_ab)]
    rng.shuffle(versions)
    order: dict[int, str] = {}
    for i, (label, _content) in enumerate(versions, 1):
        order[i] = label
    return order


def aggregate_rankings(
    rankings: list[list[str] | None],
    labels: list[str] | None = None,
    tiebreak_winner: str | None = "A",
) -> tuple[str, dict[str, int], int]:
    """Borda aggregation with conservative tiebreak.

    For each label in position `p` of a judge's ranking, add `(n - p)` points
    where `n = len(labels)`. Returns `(winner, scores_dict, n_valid_judges)`.

    Tiebreak: `tiebreak_winner` gets priority 0; all others get 1+index. This
    gives the incumbent (A) priority when tied with B or AB (conservative bias).
    """
    if labels is None:
        labels = ["A", "B", "AB"]
    scores: dict[str, int] = {label: 0 for label in labels}
    n = len(labels)
    valid = [r for r in rankings if r is not None]
    for ranking in valid:
        for pos, label in enumerate(ranking):
            if label in scores and pos < n:
                scores[label] += n - pos
    if tiebreak_winner:
        priority = {
            label: (0 if label == tiebreak_winner else i + 1)
            for i, label in enumerate(labels)
        }
    else:
        priority = {label: i for i, label in enumerate(labels)}
    ranked = sorted(scores.keys(), key=lambda k: (-scores[k], priority[k]))
    return ranked[0], scores, len(valid)


# ── Tournament ──────────────────────────────────────────────────────────


class Tournament(Generic[T]):
    """Run the self-refinement convergence loop over an arbitrary content type T."""

    def __init__(
        self,
        handler: ContentHandler[T],
        client: LLMClient,
        cfg: TournamentConfig,
        artifact_dir: Path,
        rng: random.Random | None = None,
        judge_plugins: list[Any] | None = None,
    ) -> None:
        self.handler = handler
        self.client = client
        self.cfg = cfg
        self.artifact_dir = artifact_dir
        self.rng = rng if rng is not None else random.Random()
        self.store = TournamentArtifactStore(artifact_dir)
        self.log = get_logger(component="tournament", artifact_dir=str(artifact_dir))
        self._sem = asyncio.Semaphore(max(1, cfg.max_parallel_subprocesses))
        # Optional list of JudgeProviderPlugin instances to supplement LLM judges.
        self._judge_plugins: list[Any] = judge_plugins or []

    async def run(self, task_prompt: str, initial: T) -> tuple[T, list[PassResult]]:
        """Run passes 1..max_rounds, converge when streak >= convergence_k.

        Writes initial_a, per-pass artifacts, incumbent_after_NN for each
        non-A win, and final_output + history.json at exit. Returns the final
        incumbent and the full pass history.
        """
        self.store.write_initial(self.handler.render_as_markdown(initial))
        self.log.info(
            "tournament_start",
            max_rounds=self.cfg.max_rounds,
            convergence_k=self.cfg.convergence_k,
            num_judges=self.cfg.num_judges,
        )

        incumbent: T = initial
        history: list[PassResult] = []
        streak = 0

        for pass_num in range(1, self.cfg.max_rounds + 1):
            winner, new_incumbent, result = await self.run_pass(
                task_prompt, incumbent, pass_num
            )
            history.append(result)

            if winner == "A":
                streak += 1
            else:
                streak = 0
                incumbent = new_incumbent
                self.store.write_incumbent_after(
                    pass_num, self.handler.render_as_markdown(incumbent)
                )

            self.log.info(
                "pass_complete",
                pass_num=pass_num,
                winner=winner,
                scores=result.scores,
                valid_judges=result.valid_judges,
                streak=streak,
            )

            if streak >= self.cfg.convergence_k:
                self.log.info("converged", pass_num=pass_num, streak=streak)
                break

        self.store.write_final(self.handler.render_as_markdown(incumbent), history)
        return incumbent, history

    async def run_pass(
        self, task_prompt: str, incumbent: T, pass_num: int
    ) -> tuple[WinnerLabel, T, PassResult]:
        """CRITIC -> ARCHITECT_B -> SYNTHESIZER -> N parallel JUDGES -> Borda."""
        hash_before = self.handler.hash(incumbent)
        t0 = time.time()
        model = self.cfg.model

        # Render incumbent text once for serialization
        version_a_md = self.handler.render_as_markdown(incumbent)

        # 1. Critic
        critic_user = self.handler.render_for_critic(incumbent, task_prompt)
        critic_text = await self.client.call(
            system=CRITIC_SYSTEM, user=critic_user, role="critic_t", model=model
        )

        # 2. Architect B
        architect_b_user = self.handler.render_for_architect_b(
            task_prompt, incumbent, critic_text
        )
        revision_text = await self.client.call(
            system=ARCHITECT_B_SYSTEM,
            user=architect_b_user,
            role="architect_b",
            model=model,
        )
        v_b: T = self.handler.parse_revision(revision_text, incumbent)

        # 3. Synthesizer — coin-flip X/Y ordering via tournament RNG
        if self.rng.random() < 0.5:
            v_x, v_y = incumbent, v_b
        else:
            v_x, v_y = v_b, incumbent
        synth_user = self.handler.render_for_synthesizer(task_prompt, v_x, v_y)
        synth_text = await self.client.call(
            system=SYNTHESIZER_SYSTEM,
            user=synth_user,
            role="synthesizer",
            model=model,
        )
        v_ab: T = self.handler.parse_synthesis(synth_text, incumbent, v_b)

        # 4. N parallel judges with randomized presentation order
        rankings, judge_details = await self._run_judges(
            task_prompt, incumbent, v_b, v_ab, model
        )

        # 5. Borda aggregation with conservative tiebreak to A
        tiebreak = "A" if self.cfg.conservative_tiebreak else None
        winner, scores, valid_judges = aggregate_rankings(
            rankings, labels=["A", "B", "AB"], tiebreak_winner=tiebreak
        )

        version_b_md = self.handler.render_as_markdown(v_b)
        version_ab_md = self.handler.render_as_markdown(v_ab)
        elapsed = time.time() - t0
        winners_map: dict[str, T] = {"A": incumbent, "B": v_b, "AB": v_ab}
        chosen = winners_map[winner]
        hash_after = self.handler.hash(chosen)

        result = PassResult(
            pass_num=pass_num,
            winner=winner,  # type: ignore[arg-type]
            scores=scores,
            valid_judges=valid_judges,
            elapsed_s=round(elapsed, 3),
            judge_details=judge_details,
            incumbent_hash_before=hash_before,
            incumbent_hash_after=hash_after,
            meta={"timestamp": time.time()},
        )

        self.store.write_pass(
            pass_num=pass_num,
            version_a_md=version_a_md,
            critic_md=critic_text,
            version_b_md=version_b_md,
            version_ab_md=version_ab_md,
            result=result,
        )

        return winner, chosen, result  # type: ignore[return-value]

    async def _run_judges(
        self,
        task_prompt: str,
        v_a: T,
        v_b: T,
        v_ab: T,
        model: str | None,
    ) -> tuple[list[list[str] | None], list[dict[str, Any]]]:
        """Spawn N judges concurrently (capped by semaphore), parse rankings.

        After LLM judges complete, also invokes any registered
        :class:`~plugins.registry.JudgeProviderPlugin` instances.  Each plugin
        returns a permutation of ``[0, 1, 2]`` (best-to-worst indices into
        ``[v_a, v_b, v_ab]``), which is validated then mapped to canonical
        labels (0→"A", 1→"B", 2→"AB") before being added to the Borda tally.
        """
        orders: list[dict[int, str]] = []
        coros = []
        for _ in range(self.cfg.num_judges):
            order = randomize_for_judge(v_a, v_b, v_ab, self.rng)
            orders.append(order)
            user = self.handler.render_for_judge(task_prompt, v_a, v_b, v_ab, order)
            coros.append(self._guarded_judge(user, model))

        responses = await asyncio.gather(*coros, return_exceptions=True)

        rankings: list[list[str] | None] = []
        judge_details: list[dict[str, Any]] = []
        for resp, order in zip(responses, orders):
            if isinstance(resp, BaseException):
                rankings.append(None)
                judge_details.append(
                    {"error": str(resp), "order": {str(k): v for k, v in order.items()}}
                )
                continue
            raw_ranking = parse_ranking(resp, "123")
            if raw_ranking is None:
                rankings.append(None)
                judge_details.append(
                    {
                        "ranking": None,
                        "order": {str(k): v for k, v in order.items()},
                        "raw_response": resp,
                    }
                )
            else:
                mapped = [order.get(int(r), r) for r in raw_ranking]
                rankings.append(mapped)
                judge_details.append(
                    {
                        "ranking": mapped,
                        "order": {str(k): v for k, v in order.items()},
                        "raw_response": resp,
                    }
                )

        # Invoke JudgeProviderPlugin instances and merge into Borda tally.
        _index_to_label: dict[int, str] = {0: "A", 1: "B", 2: "AB"}
        _valid_permutation = {0, 1, 2}
        versions = [v_a, v_b, v_ab]
        for plugin in self._judge_plugins:
            try:
                raw_indices = await plugin.rank(task_prompt, versions)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "tournament.plugin_judge_error",
                    plugin=getattr(plugin, "name", repr(plugin)),
                    error=str(exc),
                )
                rankings.append(None)
                judge_details.append(
                    {
                        "plugin": getattr(plugin, "name", repr(plugin)),
                        "error": str(exc),
                    }
                )
                continue

            # Validate: must be a permutation of [0, 1, 2].
            if (
                not isinstance(raw_indices, list)
                or len(raw_indices) != 3
                or set(raw_indices) != _valid_permutation
            ):
                self.log.warning(
                    "tournament.plugin_judge_invalid",
                    plugin=getattr(plugin, "name", repr(plugin)),
                    raw=raw_indices,
                )
                rankings.append(None)
                judge_details.append(
                    {
                        "plugin": getattr(plugin, "name", repr(plugin)),
                        "ranking": None,
                        "raw": raw_indices,
                        "error": "invalid permutation",
                    }
                )
                continue

            mapped = [_index_to_label[i] for i in raw_indices]
            rankings.append(mapped)
            judge_details.append(
                {
                    "plugin": getattr(plugin, "name", repr(plugin)),
                    "ranking": mapped,
                }
            )

        return rankings, judge_details

    async def _guarded_judge(self, user: str, model: str | None) -> str:
        """Run a judge call under the concurrency semaphore."""
        async with self._sem:
            return await self.client.call(
                system=JUDGE_SYSTEM, user=user, role="judge", model=model
            )


__all__ = [
    "ContentHandler",
    "LLMClient",
    "PassResult",
    "Tournament",
    "TournamentConfig",
    "TournamentError",
    "WinnerLabel",
    "aggregate_rankings",
    "parse_ranking",
    "randomize_for_judge",
]
