"""Framing/altitude phase (ADR-0044).

Inserted between exploration and planning in ``run_plan_phase``: classifies a
defect as ``local_defect`` vs ``realized_design_failure`` (deterministic signals
gate + one conservative LLM call) and, on the design path, generates altitude-
diverse strategies selected by the ``altitude_judge`` Borda panel with minimality
suspended. The winner is handed to the architect, where minimality resumes.

Scaffolding only in Phase 0; the skeleton/signals/classifier land in Phase 2,
multi-approach generation in Phase 3, and the altitude panel in Phase 4.
"""

from __future__ import annotations
