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
from dataclasses import dataclass, field
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
    # Optional runaway detector (Fix 6). Both ``None`` → feature disabled.
    # ``score_stability_window`` is the number of trailing passes to compare;
    # ``score_stability_max_delta`` is the maximum allowed sum of |Δscore|
    # across A/B/AB between the first and last pass in the window. When the
    # window is full and the delta is below the threshold, the engine emits
    # ``tournament.runaway_detected`` and breaks out of the loop with the
    # current incumbent.
    score_stability_window: int | None = None
    score_stability_max_delta: int | None = None


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


@dataclass
class PartialPassState:
    """In-progress pass state recovered from on-disk per-role artifacts.

    Tier 3 mid-pass checkpointing: each role writes its output to disk as it
    completes. On resume, this struct surfaces which roles already finished so
    :meth:`Tournament.run_pass` can skip them and reuse their on-disk outputs.

    Field semantics:
        - ``version_a_md``: input incumbent markdown (always present in a
          partial dir, since it's the first thing written).
        - ``critic_md``: set iff CRITIC completed.
        - ``version_b_md``: set iff ARCHITECT_B completed.
        - ``version_ab_md`` + ``synth_meta``: set iff SYNTHESIZER completed
          (both written together, so populated as a pair).
        - ``judge_orders``: judge_index → order map (any subset).
        - ``judge_responses``: judge_index → response (any subset of those
          with a recorded order).
    """

    pass_num: int
    version_a_md: str | None
    critic_md: str | None
    version_b_md: str | None
    version_ab_md: str | None
    synth_meta: dict[str, str] | None
    judge_orders: dict[int, dict[int, str]] = field(default_factory=dict)
    judge_responses: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ResumeState:
    """Resume metadata recovered from a tournament's on-disk artifacts.

    Returned by :meth:`tournament.state.TournamentArtifactStore.read_resume_state`.
    The caller (:meth:`Tournament.run`) decides whether to:
        - short-circuit (``completed=True``) and return ``final_md`` directly, or
        - continue the loop from ``starting_pass_num`` with the recovered
          ``incumbent_md`` and trailing-A-wins ``streak``.
        - if ``partial`` is populated, the first iteration of the resumed loop
          skips already-completed roles within the in-progress pass.
    """

    starting_pass_num: int  # next pass to run (1-indexed)
    incumbent_md: str  # the markdown to use as A
    streak: int  # trailing A-wins for convergence accounting
    completed: bool  # if True, tournament already converged → caller short-circuits
    final_md: str | None  # populated iff completed
    partial: "PartialPassState | None" = None  # Tier 3: in-progress pass state


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


def _score_window_stable(
    history: list["PassResult"], window: int, max_delta: int
) -> bool:
    """Return True if the trailing ``window`` passes have near-stable scores.

    "Stable" means the sum of absolute deltas across the A/B/AB scores
    between ``history[-window]`` and ``history[-1]`` is ≤ ``max_delta``.
    Caller must ensure ``len(history) >= window`` and ``window >= 1``.
    """
    first = history[-window].scores
    last = history[-1].scores
    total = 0
    for label in ("A", "B", "AB"):
        total += abs(last.get(label, 0) - first.get(label, 0))
    return total <= max_delta


def _describe_skipped(partial: PartialPassState) -> list[str]:
    """Return the list of role names already complete in a partial pass."""
    skipped: list[str] = []
    if partial.critic_md is not None:
        skipped.append("critic_t")
    if partial.version_b_md is not None:
        skipped.append("architect_b")
    if partial.version_ab_md is not None and partial.synth_meta is not None:
        skipped.append("synthesizer")
    if partial.judge_responses:
        for idx in sorted(partial.judge_responses.keys()):
            skipped.append(f"judge_{idx}")
    return skipped


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

        Resume semantics (Tier 2D):
            - If on-disk artifacts indicate a completed tournament
              (``final_output.md`` present), return the final markdown without
              any LLM calls.
            - If on-disk artifacts indicate partial progress, resume from the
              next unfinished pass with the recovered incumbent + streak.
            - If the on-disk ``initial_a.md`` hash doesn't match the current
              ``initial`` argument, log a warning and start fresh.
        """
        resume = self.store.read_resume_state()
        if resume is not None and resume.completed:
            self.log.info(
                "tournament_resumed_completed",
                final_bytes=len(resume.final_md or ""),
            )
            history_raw = self.store.read_history() or []
            history_loaded: list[PassResult] = []
            for entry in history_raw:
                try:
                    history_loaded.append(PassResult.model_validate(entry))
                except Exception:  # noqa: BLE001
                    # If a stale/malformed history entry is present, skip it.
                    pass
            final_text = resume.final_md or ""
            return self.handler.parse_revision(final_text, initial), history_loaded

        # If a fresh-but-mismatched initial is detected, fall back to a fresh
        # start. Otherwise keep the resume context.
        if resume is not None:
            on_disk_initial = self.store.read_initial()
            if on_disk_initial is not None:
                on_disk_t = self.handler.parse_revision(on_disk_initial, initial)
                if self.handler.hash(on_disk_t) != self.handler.hash(initial):
                    self.log.warning(
                        "tournament_resume_initial_mismatch",
                        action="starting_fresh",
                    )
                    resume = None

        if resume is None:
            self.store.write_initial(self.handler.render_as_markdown(initial))
            incumbent: T = initial
            streak = 0
            start_pass = 1
        else:
            # Skip write_initial — the on-disk file is already authoritative.
            incumbent = self.handler.parse_revision(resume.incumbent_md, initial)
            streak = resume.streak
            start_pass = resume.starting_pass_num
            self.log.info(
                "tournament_resumed",
                from_pass=start_pass,
                streak=streak,
            )

            # Tier 3: stale-version_a guard. If a partial pass dir's recorded
            # version_a doesn't match the recovered incumbent, downstream
            # artifacts (critic, version_b, ...) are stale — discard partial.
            if resume.partial is not None and resume.partial.version_a_md is not None:
                recovered_t = self.handler.parse_revision(
                    resume.partial.version_a_md, initial
                )
                if self.handler.hash(recovered_t) != self.handler.hash(incumbent):
                    self.log.warning(
                        "tournament.partial_resume_stale",
                        pass_num=resume.partial.pass_num,
                        reason="version_a_hash_mismatch",
                        action="discarding_partial",
                    )
                    resume = ResumeState(
                        starting_pass_num=resume.starting_pass_num,
                        incumbent_md=resume.incumbent_md,
                        streak=resume.streak,
                        completed=resume.completed,
                        final_md=resume.final_md,
                        partial=None,
                    )

        self.log.info(
            "tournament_start",
            max_rounds=self.cfg.max_rounds,
            convergence_k=self.cfg.convergence_k,
            num_judges=self.cfg.num_judges,
        )

        history: list[PassResult] = []

        # If we resumed with an A-win streak already meeting convergence,
        # short-circuit before the loop body. (Equivalent to the "already
        # completed" path but reached via partial state rather than
        # ``final_output.md``.)
        if streak >= self.cfg.convergence_k:
            self.log.info("converged", pass_num=start_pass - 1, streak=streak)
            self.store.write_final(
                self.handler.render_as_markdown(incumbent), history
            )
            return incumbent, history

        for pass_num in range(start_pass, self.cfg.max_rounds + 1):
            # Pass `partial` only into the first iteration of the resumed pass.
            pass_partial = (
                resume.partial
                if (resume is not None and pass_num == start_pass)
                else None
            )
            if pass_partial is not None:
                self.log.info(
                    "tournament.partial_pass_resume",
                    pass_num=pass_num,
                    roles_skipped=_describe_skipped(pass_partial),
                )
            winner, new_incumbent, result = await self.run_pass(
                task_prompt, incumbent, pass_num, partial=pass_partial
            )

            # Fix 1: hash-equality short-circuit. If the chosen variant is
            # byte-identical to the incumbent (e.g. synthesizer regurgitated
            # incumbent unchanged), treat the pass as an A-win for streak
            # purposes regardless of the Borda label. This breaks the
            # "AB wins forever but content is stable" deadlock observed in
            # the plan-47a530bd run.
            no_change = (
                result.incumbent_hash_before == result.incumbent_hash_after
            )
            effective_winner: WinnerLabel = "A" if no_change else winner
            result.meta["effective_winner"] = effective_winner
            if no_change and winner != "A":
                self.log.info(
                    "tournament.no_change_winner",
                    pass_num=pass_num,
                    raw_winner=winner,
                    effective_winner=effective_winner,
                )
            # Re-persist the pass result so the on-disk artifact reflects the
            # in-memory ``effective_winner``. Mirrors the runaway-detector
            # branch below; idempotent re-write of the same pass dir's
            # ``result.json``.
            self.store.write_pass_result(pass_num, result)

            history.append(result)

            if effective_winner == "A":
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
                effective_winner=effective_winner,
                scores=result.scores,
                valid_judges=result.valid_judges,
                streak=streak,
            )

            if streak >= self.cfg.convergence_k:
                self.log.info("converged", pass_num=pass_num, streak=streak)
                break

            # Fix 6: runaway detector. If both knobs are configured and the
            # trailing-window scores haven't moved by more than max_delta, we
            # are stuck producing the same Borda outcome each pass — abort
            # rather than burn the remaining max_rounds on identical
            # synthesizer-style refinements.
            window = self.cfg.score_stability_window
            max_delta = self.cfg.score_stability_max_delta
            if (
                window is not None
                and max_delta is not None
                and len(history) >= window
                and _score_window_stable(history, window, max_delta)
            ):
                first_scores = history[-window].scores
                last_scores = history[-1].scores
                total_delta = sum(
                    abs(last_scores.get(k, 0) - first_scores.get(k, 0))
                    for k in ("A", "B", "AB")
                )
                self.log.warning(
                    "tournament.runaway_detected",
                    pass_num=pass_num,
                    window=window,
                    total_delta=total_delta,
                    max_delta=max_delta,
                )
                result.meta["runaway_detected"] = True
                # Re-persist the pass result with the runaway annotation so
                # on-disk artifacts surface the early termination cause.
                self.store.write_pass_result(pass_num, result)
                break

        self.store.write_final(self.handler.render_as_markdown(incumbent), history)
        return incumbent, history

    async def run_pass(
        self,
        task_prompt: str,
        incumbent: T,
        pass_num: int,
        partial: PartialPassState | None = None,
    ) -> tuple[WinnerLabel, T, PassResult]:
        """CRITIC -> ARCHITECT_B -> SYNTHESIZER -> N parallel JUDGES -> Borda.

        Tier 3: each role's output is checkpointed to disk on completion. If
        ``partial`` is provided, already-completed roles are skipped and their
        on-disk outputs reused (skipped roles do NOT draw from the RNG).
        """
        hash_before = self.handler.hash(incumbent)
        t0 = time.time()
        model = self.cfg.model

        # Render incumbent text once for serialization. Always re-write
        # version_a.md — it's idempotent and ensures the partial-resume guard
        # has a stable record of the incumbent at pass start.
        version_a_md = self.handler.render_as_markdown(incumbent)
        self.store.write_version_a(pass_num, version_a_md)

        # 1. CRITIC
        if partial is not None and partial.critic_md is not None:
            critic_text = partial.critic_md
            self.log.info(
                "tournament.role_skipped", pass_num=pass_num, role="critic_t"
            )
        else:
            critic_user = self.handler.render_for_critic(incumbent, task_prompt)
            critic_text = await self.client.call(
                system=CRITIC_SYSTEM, user=critic_user, role="critic_t", model=model
            )
            self.store.write_critic(pass_num, critic_text)

        # 2. ARCHITECT_B
        if partial is not None and partial.version_b_md is not None:
            v_b: T = self.handler.parse_revision(partial.version_b_md, incumbent)
            self.log.info(
                "tournament.role_skipped", pass_num=pass_num, role="architect_b"
            )
        else:
            architect_b_user = self.handler.render_for_architect_b(
                task_prompt, incumbent, critic_text
            )
            revision_text = await self.client.call(
                system=ARCHITECT_B_SYSTEM,
                user=architect_b_user,
                role="architect_b",
                model=model,
            )
            v_b = self.handler.parse_revision(revision_text, incumbent)
            self.store.write_version_b(
                pass_num, self.handler.render_as_markdown(v_b)
            )

        # 3. SYNTHESIZER — coin-flip X/Y ordering via tournament RNG.
        # On partial-resume with both version_ab.md AND synth_meta on disk, we
        # skip the entire synth block (including the coin flip — this is the
        # documented determinism asymmetry).
        if (
            partial is not None
            and partial.version_ab_md is not None
            and partial.synth_meta is not None
        ):
            v_ab: T = self.handler.parse_synthesis(
                partial.version_ab_md, incumbent, v_b
            )
            synth_meta = partial.synth_meta
            self.log.info(
                "tournament.role_skipped", pass_num=pass_num, role="synthesizer"
            )
        else:
            if self.rng.random() < 0.5:
                v_x, v_y = incumbent, v_b
                synth_meta = {"x_label": "A", "y_label": "B"}
            else:
                v_x, v_y = v_b, incumbent
                synth_meta = {"x_label": "B", "y_label": "A"}
            synth_user = self.handler.render_for_synthesizer(
                task_prompt, v_x, v_y
            )
            synth_text = await self.client.call(
                system=SYNTHESIZER_SYSTEM,
                user=synth_user,
                role="synthesizer",
                model=model,
            )
            v_ab = self.handler.parse_synthesis(synth_text, incumbent, v_b)
            self.store.write_synthesis(
                pass_num, self.handler.render_as_markdown(v_ab), synth_meta
            )

        # 4. N parallel judges with randomized presentation order
        partial_judges: tuple[
            dict[int, dict[int, str]] | None,
            dict[int, dict[str, Any]] | None,
        ]
        if partial is not None:
            partial_judges = (partial.judge_orders, partial.judge_responses)
        else:
            partial_judges = (None, None)

        rankings, judge_details = await self._run_judges(
            task_prompt,
            incumbent,
            v_b,
            v_ab,
            model,
            pass_num=pass_num,
            partial_judges=partial_judges,
        )

        # 5. Borda aggregation with conservative tiebreak to A
        tiebreak = "A" if self.cfg.conservative_tiebreak else None
        winner, scores, valid_judges = aggregate_rankings(
            rankings, labels=["A", "B", "AB"], tiebreak_winner=tiebreak
        )

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

        self.store.write_pass_result(pass_num, result)

        return winner, chosen, result  # type: ignore[return-value]

    async def _run_judges(
        self,
        task_prompt: str,
        v_a: T,
        v_b: T,
        v_ab: T,
        model: str | None,
        *,
        pass_num: int,
        partial_judges: tuple[
            dict[int, dict[int, str]] | None,
            dict[int, dict[str, Any]] | None,
        ] = (None, None),
    ) -> tuple[list[list[str] | None], list[dict[str, Any]]]:
        """Spawn N judges concurrently (capped by semaphore), parse rankings.

        Tier 3: each judge's order is checkpointed to disk before its LLM
        call; its response is checkpointed after the call completes. On
        partial resume:
          * judges with a recorded response → reuse without an LLM call.
          * judges with a recorded order but no response → re-run with the
            recorded order (don't re-shuffle).
          * judges with neither → fresh shuffle + LLM call.

        After LLM judges complete, also invokes any registered
        :class:`~plugins.registry.JudgeProviderPlugin` instances.  Each plugin
        returns a permutation of ``[0, 1, 2]`` (best-to-worst indices into
        ``[v_a, v_b, v_ab]``), which is validated then mapped to canonical
        labels (0→"A", 1→"B", 2→"AB") before being added to the Borda tally.
        """
        recorded_orders, recorded_responses = partial_judges
        recorded_orders = recorded_orders or {}
        recorded_responses = recorded_responses or {}

        # Build per-judge plan: order, fresh-or-recorded flag.
        orders: list[dict[int, str]] = []
        # task_meta entries: ("recorded", response_dict) or ("fresh", None)
        task_meta: list[tuple[str, dict[str, Any] | None]] = []
        coros: list[Any] = []
        for i in range(self.cfg.num_judges):
            recorded_resp = recorded_responses.get(i)
            recorded_order = recorded_orders.get(i)

            if recorded_resp is not None and recorded_order is not None:
                # Reuse recorded response, skip LLM call entirely.
                self.log.info(
                    "tournament.judge_call_skipped",
                    pass_num=pass_num,
                    judge_index=i,
                )
                orders.append(recorded_order)
                task_meta.append(("recorded", recorded_resp))
                continue

            if recorded_order is not None:
                # Recorded order with no response — re-run judge with same order.
                # Do NOT draw from RNG (preserves the recorded order).
                self.log.info(
                    "tournament.judge_order_reused",
                    pass_num=pass_num,
                    judge_index=i,
                )
                order = recorded_order
            else:
                # Fresh judge: shuffle and persist order before LLM call.
                order = randomize_for_judge(v_a, v_b, v_ab, self.rng)

            orders.append(order)
            task_meta.append(("fresh", None))
            user = self.handler.render_for_judge(task_prompt, v_a, v_b, v_ab, order)
            coros.append(
                self._guarded_judge(user, model, pass_num, i, order)
            )

        # Run only the fresh-call judges; map their responses back into the
        # full per-judge response list.
        gathered = await asyncio.gather(*coros, return_exceptions=True)
        gathered_iter = iter(gathered)
        responses: list[Any] = []
        for kind, recorded in task_meta:
            if kind == "recorded":
                # Synthesize a `resp` value identical to what the LLM would
                # have returned (the raw text), so downstream parsing works.
                responses.append(recorded.get("raw") if recorded else None)
            else:
                responses.append(next(gathered_iter))

        rankings: list[list[str] | None] = []
        judge_details: list[dict[str, Any]] = []
        for i, (resp, order) in enumerate(zip(responses, orders)):
            kind, recorded = task_meta[i]
            if kind == "recorded" and recorded is not None:
                # Recorded ranking is the slot-index list (e.g., ["1","2","3"])
                # written by ``_guarded_judge`` (via parse_ranking). Map back to
                # canonical labels through the recorded order.
                rec_ranking = recorded.get("ranking")
                rec_error = recorded.get("error")
                if rec_error is not None or rec_ranking is None:
                    rankings.append(None)
                    judge_details.append(
                        {
                            "error": rec_error or "no ranking recorded",
                            "order": {str(k): v for k, v in order.items()},
                        }
                    )
                else:
                    mapped = [order.get(int(r), r) for r in rec_ranking]
                    rankings.append(mapped)
                    judge_details.append(
                        {
                            "ranking": mapped,
                            "order": {str(k): v for k, v in order.items()},
                            "raw_response": recorded.get("raw", ""),
                        }
                    )
                continue

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

    async def _guarded_judge(
        self,
        user: str,
        model: str | None,
        pass_num: int,
        judge_index: int,
        order: dict[int, str],
    ) -> str:
        """Run a judge call under the concurrency semaphore.

        Tier 3: persists the shuffle order to disk BEFORE the LLM call so a
        crash mid-call leaves the order on disk for the resumed run; persists
        the parsed response to disk AFTER the call lands so a missing
        ``response.json`` indicates a still-pending judge.
        """
        async with self._sem:
            self.store.write_judge_order(pass_num, judge_index, order)
            response_text = await self.client.call(
                system=JUDGE_SYSTEM, user=user, role="judge", model=model
            )
            ranking = parse_ranking(response_text, "123")
            self.store.write_judge_response(
                pass_num,
                judge_index,
                {"raw": response_text, "ranking": ranking, "error": None},
            )
            return response_text


__all__ = [
    "ContentHandler",
    "LLMClient",
    "PartialPassState",
    "PassResult",
    "ResumeState",
    "Tournament",
    "TournamentConfig",
    "TournamentError",
    "WinnerLabel",
    "aggregate_rankings",
    "parse_ranking",
    "randomize_for_judge",
]
