"""Deterministic structural signals for the framing phase (ADR-0044).

Two structural signals feed the conservative classifier as disconfirming
evidence: recurrence-at-seam (prior human fixes touching the same files/symbols,
excluding AutoDev's own ``autodev: task`` commits) and boundary-repeatedly-touched
(how many prior AutoDev tasks fought the same boundary, from the ledger). Both
degrade-not-raise on timeout/error.

Scaffolding only in Phase 0; implemented in Phase 2.
"""

from __future__ import annotations
