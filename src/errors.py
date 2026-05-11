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


class TournamentAdapterMismatchError(ConfigError):
    """Tournaments are enabled but the resolved adapter cannot fan out.

    Raised when ``platform`` resolves to ``inline`` but at least one
    tournament (plan / impl / phase_review) is enabled. Tournaments
    spawn IAG-isolated branches and N judges in parallel via
    ``adapter.parallel()``; :class:`InlineAdapter` is single-process by
    construction (its ``parallel()`` raises ``NotImplementedError``).
    The two are architecturally incompatible. The fix is to set
    ``platform: claude_code`` (or ``cursor``) in ``.autodev/config.json``,
    or to disable tournaments under ``cfg.tournaments.<phase>.enabled``.
    """

    def __init__(self, enabled_phases: list[str]) -> None:
        self.enabled_phases = list(enabled_phases)
        super().__init__(
            "Tournaments are enabled ("
            + ", ".join(enabled_phases)
            + ") but the resolved adapter is InlineAdapter, which is "
            "inherently sequential. Tournaments fan out IAG-isolated "
            "branches and judges in parallel via `adapter.parallel()`. "
            "Fix one of:\n"
            "  - Set `platform: claude_code` (or `cursor`) in "
            "`.autodev/config.json`, OR\n"
            "  - Disable tournaments via "
            "`tournaments.plan.enabled: false`, "
            "`tournaments.impl.enabled: false`, and "
            "`tournaments.phase_review.enabled: false`."
        )


class GuardrailExceededError(AutodevError):
    """Task exceeded a configured budget (tool calls, duration, diff size)."""


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
