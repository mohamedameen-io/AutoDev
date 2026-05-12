"""Typed exception hierarchy for autodev."""


class AutodevError(Exception):
    """Base class for all autodev-raised exceptions."""


class AdapterError(AutodevError):
    """Platform adapter failure (subprocess, parsing, unreachable CLI)."""


class TournamentError(AutodevError):
    """Tournament engine failure (judge parse, convergence stall)."""


class LedgerCorruptError(AutodevError):
    """Append-only ledger integrity violation (CAS mismatch, bad JSON)."""


class PlanConcurrentModificationError(AutodevError):
    """Plan mutation attempted with stale base hash."""


class ConfigError(AutodevError):
    """Invalid or missing `.autodev/config.json`."""


# v0.26.0: ``TournamentAdapterMismatchError`` was deleted. It existed to
# guard the InlineAdapter ↔ tournaments mismatch (inline is single-process,
# tournaments fan out in parallel). With InlineAdapter gone, the mismatch
# is unrepresentable.


class GuardrailExceededError(AutodevError):
    """Task exceeded a configured budget (tool calls, duration, diff size)."""


class DiffParseError(AutodevError):
    """Developer output had non-empty diff text with no parseable headers.

    v0.27.0: previously a malformed/garbage ``diff`` body silently returned
    ``[]`` from :func:`adapters.git_utils.extract_files_from_diff`. Diff-scoped
    QA gates (secretscan, hallucination_guard, mutation_test, code_size) then
    saw ``paths=[]`` and trivially passed — a silent skip that masked real
    regressions (e.g. a corrupt diff body shipped via a stop-truncation).
    Strict callers now raise this typed error so :func:`_run_qa_gates` can
    fail-closed against tasks declared as ``produces_diff=True``.
    """


class EmptyDiffScopeError(AutodevError):
    """Diff parsed cleanly but yielded zero files for a diff-producing task.

    Distinct from :class:`DiffParseError` so callers can distinguish "garbage
    in" (parser couldn't extract anything) from "developer returned an empty
    but well-formed diff" (the developer didn't actually write any code).
    """


class PhaseStuckError(AutodevError):
    """v0.22.2 B2: phase has no pending tasks but tasks are non-terminal.

    Raised by ``_execute_phase_dag`` when its dispatcher exits with tasks
    wedged in states like ``coded`` / ``in_progress`` / ``reviewed`` (i.e.
    NOT in ``_TERMINAL_TASK_STATUSES`` and NOT in ``pending``). Pre-B2
    this returned silently, making interrupted runs look like clean
    completions — surfaced by D-2 in the 2026-05-09 Unity stall
    investigation.

    Operators should normally run ``autodev resume``, which v0.22.2 B1
    pairs with a ``reap_orphans()`` reset on resume. The exception
    carries ``phase_id`` and ``stuck_task_ids`` so the surfaced message
    points at the offending tasks directly.
    """

    def __init__(self, phase_id: str, stuck_task_ids: list[str]) -> None:
        self.phase_id = phase_id
        self.stuck_task_ids = list(stuck_task_ids)
        super().__init__(
            f"Phase {phase_id!r} has no pending tasks but "
            f"{len(self.stuck_task_ids)} task(s) remain non-terminal: "
            f"{self.stuck_task_ids!r}. This usually indicates an "
            f"interrupted run; re-running `autodev resume` will reap "
            f"orphan in-flight tasks (v0.22.2 B1)."
        )
