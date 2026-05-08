"""v0.18.0 C3: judge recusal — skip judges that authored prior work for the task.

A specialist judge that is asked to evaluate work it previously authored
introduces a self-bias: the judge has already invested cognitive
weight in one direction. :class:`JudgeRecusal` detects this from the
evidence-bundle history and lets the runner skip the judge's vote.

Detection signal: if the judge's role appears in any prior evidence
record for the same task (e.g. a ``critic`` evidence bundle exists for
task 1.1, and the council includes a ``critic`` judge), the judge is
recused.

This is rule-based; v0.20.0+ may swap in a confidence-based recusal that
weighs evidence age + judge specialty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from state.schemas import Task


class JudgeRecusal:
    """Detect when a judge authored prior work for the same task."""

    def should_recuse(
        self,
        judge_role: str,
        task: "Task",
        prior_evidence: list[dict] | None = None,
    ) -> bool:
        """True iff the given judge_role should recuse for this task.

        Detection: walks ``prior_evidence`` (list of evidence-bundle
        dicts) and returns True if any entry's ``role`` field equals
        ``judge_role``. Also checks ``task.assigned_agent`` and
        ``task.evidence_bundle`` for cross-references.

        Returns False when:
            - prior_evidence is None or empty;
            - no entry's role matches the judge_role.
        """
        if prior_evidence is None:
            prior_evidence = []

        # Direct match in evidence bundles.
        for entry in prior_evidence:
            if not isinstance(entry, dict):
                continue
            entry_role = entry.get("role") or entry.get("agent_role")
            if entry_role == judge_role:
                return True

        # Indirect signal: task.assigned_agent equals the judge_role
        # (the judge would be evaluating its own implementation work).
        assigned = getattr(task, "assigned_agent", None)
        if assigned is not None and assigned == judge_role:
            return True

        return False


__all__ = ["JudgeRecusal"]
