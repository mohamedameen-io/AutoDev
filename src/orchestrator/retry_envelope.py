"""v0.27 Phase 4: typed retry envelope for architect-recovery loops.

The architect-retry pipeline (``_validate_with_persistent_drop`` in
:mod:`orchestrator.plan_phase`) feeds the architect a structured
context block on every retry attempt — the prior attempt's markdown,
the stringified exception, the accumulated ``prior_errors`` list, and
optionally the typed ``path_error_*`` fields when the failure is a
:class:`orchestrator.path_validator.PathValidationError`.

v0.26.2 built this context as a plain ``dict[str, Any]`` inside
:func:`orchestrator.plan_phase._build_retry_env`. Two problems:

  1. The set of keys was inlined in the function body, so adding a
     new field (e.g. v0.27 Phase 4's parse-error / pyd-error typed
     routing) had to be done in three places.
  2. The keys were untyped — typos like ``path_error_resaon`` would
     silently survive into the retry envelope and break the
     architect's downstream parser.

This module replaces that dict with the :class:`TypedRetryEnvelope`
Pydantic model. Behaviour is unchanged in v0.27.0: the existing
:func:`orchestrator.plan_phase._build_retry_env` constructs a
``TypedRetryEnvelope`` and serialises it back to a ``dict`` so the
``DelegationEnvelope.context`` field continues to carry the same JSON
shape on the wire. Phase 4 (Commit 7) extends the model with new
fields without touching the call site.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PriorError(BaseModel):
    """One entry in :attr:`TypedRetryEnvelope.prior_errors`.

    Records ``(raw, reason, count)`` so the architect can see "this
    same path failed N times — please correct it" rather than just
    "something failed".
    """

    model_config = ConfigDict(extra="forbid")

    raw: str
    reason: str
    count: int


class TypedRetryEnvelope(BaseModel):
    """Structured retry context fed to the architect on attempt N>1.

    Serialised via :meth:`as_context_dict` into the
    ``DelegationEnvelope.context`` payload so the wire-format remains
    a plain JSON object (no schema migration needed in v0.27.0).
    """

    model_config = ConfigDict(extra="forbid")

    # The prior architect attempt's full plan-markdown body, truncated
    # to ~2 KB so the retry envelope itself stays under the prompt's
    # context budget. ``""`` for the first retry attempt before any
    # body has been produced.
    prior_attempt: str = ""

    # Back-compat: the stringified exception that triggered the retry.
    # Always present (``""`` when no exception fired — e.g. when the
    # retry was driven by an empty plan).
    parse_error: str = ""

    # Accumulated history of distinct ``(raw, reason)`` failures across
    # all attempts on this plan-phase run. The count field lets the
    # architect prioritise repeat offenders.
    prior_errors: list[PriorError] = Field(default_factory=list)

    # Entries the orchestrator dropped from prior plan attempts via the
    # v0.26.2 persistent-failure drop mechanism. Surfacing them keeps
    # the architect aware of what's been removed so it doesn't re-emit
    # the same hedge entry on the next attempt.
    dropped_entries: list[str] = Field(default_factory=list)

    # Free-form hint text concatenated into the retry envelope's
    # prompt-renderable block. The :func:`_retry_hint_text` helper in
    # :mod:`orchestrator.plan_phase` produces this string.
    hint: str = ""

    # Typed fields populated when the most recent failure is a
    # :class:`orchestrator.path_validator.PathValidationError`. All
    # three are empty strings (not ``None``) so the wire JSON stays
    # a uniform shape.
    path_error_raw: str = ""
    path_error_reason: str = ""
    path_error_suggestion: str = ""

    def as_context_dict(self) -> dict[str, Any]:
        """Return the model as a plain ``dict[str, Any]`` for embedding
        in :attr:`DelegationEnvelope.context`.

        The serialisation goes through ``model_dump(mode="json")`` so
        every value is JSON-native — important because
        :class:`DelegationEnvelope` round-trips through
        :func:`json.dumps` when the ledger / debug path persists it.

        v0.27 wire-compat: the ``path_error_*`` keys are stripped as a
        triple when NO PathValidationError fired (raw + reason both
        empty). When at least one is set, all three are kept — the
        v0.26.2 contract was "add all three or none of them" so the
        architect's prompt template renders a consistent block.
        """
        payload = self.model_dump(mode="json")
        if not payload.get("path_error_raw") and not payload.get(
            "path_error_reason"
        ):
            payload.pop("path_error_raw", None)
            payload.pop("path_error_reason", None)
            payload.pop("path_error_suggestion", None)
        return payload


__all__ = ["PriorError", "TypedRetryEnvelope"]
