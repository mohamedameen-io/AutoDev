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


class GuardrailExceededError(AutodevError):
    """Task exceeded a configured budget (tool calls, duration, diff size)."""
