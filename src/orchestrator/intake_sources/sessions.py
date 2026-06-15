"""Prior-session gather source — mines past AutoDev runs (ADR-0045, FR2).

When AutoDev has run before in this repo, prior ``.autodev/sessions/<id>/``
snapshots + the append-only ledger may already record what was tried on the same
files (a fix that recurred, a decision that was made). This source detects that
prior-session history exists and instructs the dispatched agent to read the
relevant snapshots/ledger entries (Bash/Read over local files — no network),
distilling them into ``session``-sourced facts with the ``session-id`` as ref.
Available iff at least one prior session snapshot is on disk; otherwise skipped.
"""

from __future__ import annotations

from pathlib import Path

from autologging import get_logger

from config.schema import IntakePhaseConfig
from state.paths import ledger_path, sessions_dir
from state.schemas import GatheredFact

logger = get_logger()

# Cap on prior session ids surfaced into the prompt (keep it bounded + recent).
_MAX_SESSIONS = 10


def _prior_session_ids(cwd: Path) -> list[str]:
    """Return prior session ids (subdirs of ``.autodev/sessions/``), newest first.

    NEVER raises — a missing/unreadable dir returns ``[]``.
    """
    d = sessions_dir(cwd)
    try:
        if not d.exists():
            return []
        subs = [p for p in d.iterdir() if p.is_dir()]
    except OSError:
        return []
    subs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return [p.name for p in subs[:_MAX_SESSIONS]]


class SessionSource:
    """:class:`~orchestrator.intake_sources.GatherSource` over prior AutoDev runs."""

    name = "session"

    async def available(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> bool:
        return bool(_prior_session_ids(cwd))

    async def prepare_prompt(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> str:
        ids = _prior_session_ids(cwd)
        ledger = ledger_path(cwd)
        return (
            "AutoDev has run before in this repo. Read the prior-session\n"
            "snapshots and the ledger (local files — use Read/Bash, no network) to\n"
            "recover what was already tried on the files this task touches: a fix\n"
            "that recurred, a constraint that was recorded, a decision that was\n"
            "made. Use source `session`, ref = the session id. Emit a fact ONLY\n"
            "when it is genuinely relevant to THIS task; otherwise emit none.\n\n"
            f"Prior session ids (newest first): {', '.join(ids)}\n"
            f"Snapshots under: .autodev/sessions/<id>/snapshot.json\n"
            f"Ledger: {ledger.name} (under .autodev/)\n"
        )

    def parse(self, response: str) -> list[GatheredFact]:
        from orchestrator.intake_sources import parse_facts_for

        return parse_facts_for(response, self.name)
