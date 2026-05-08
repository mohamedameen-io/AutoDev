"""Cross-cutting host-awareness utilities.

The :mod:`runtime` namespace owns code that probes / reasons about the
*host process environment* (CPU count, memory, subprocess RSS, etc.) —
distinct from :mod:`config` (declarative knobs), :mod:`tournament`
(refinement engine), and :mod:`adapters` (LLM-call surfaces).

Today the package contains a single module:

* :mod:`runtime.resource_probe` — start-of-tournament parallelism resolver
  + per-pass adaptive ratcheting helpers. Imported by every tournament
  runner (plan / impl / phase_review) before constructing
  :class:`tournament.core.TournamentConfig`.
"""

from __future__ import annotations
