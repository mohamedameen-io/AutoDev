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


# v0.36.0 D1: per-design-class diagnosis paragraphs. Each template is
# the standalone action paragraph that prefaces the list of paths in
# that class. The architect-retry envelope renders one paragraph per
# class instead of the v0.32.0 "RECURRED N times" path-bullet list,
# which collapses N-sibling-same-class failures into one actionable
# instruction.
_DIAGNOSIS_TEMPLATES: dict[str, str] = {
    "new_md_deliverable": (
        "All `.md` paths you proposed are being rejected because they do "
        "not exist on disk yet. The plan validator requires either (a) an "
        "existing file or (b) the `[new]` prefix on every task that "
        "references the file. Choose ONE: (i) drop the documentation "
        "deliverable and embed findings in task descriptions, or (ii) tag "
        "the .md file with `[new]` on every task that touches it."
    ),
    "missing_on_disk": (
        "These paths do not exist on disk. Either correct the path to an "
        "existing file or tag the path with `[new]` on every task that "
        "references it (and on at least one creating task)."
    ),
}

# Default template for any class not explicitly listed above. Mirrors
# the missing_on_disk language since that's the most common shape.
_DEFAULT_DIAGNOSIS: str = _DIAGNOSIS_TEMPLATES["missing_on_disk"]


def diagnosis_for_class(error_class: str) -> str:
    """Return the design-class action paragraph for ``error_class``.

    Public helper so consumers (e.g. ``autodev status --blocked``) can
    surface the same actionable text the architect-retry envelope
    renders, without taking a dependency on the envelope's internals.
    """
    return _DIAGNOSIS_TEMPLATES.get(error_class, _DEFAULT_DIAGNOSIS)


class PriorError(BaseModel):
    """One entry in :attr:`TypedRetryEnvelope.prior_errors` /
    :attr:`TypedRetryEnvelope.most_recent_failures`.

    Records ``(raw, reason, count, suggestion, error_class)`` so the
    architect can see "this same path failed N times — please correct
    it" rather than just "something failed". ``suggestion`` is optional
    (empty string when none) so the wire JSON shape stays uniform
    across entries with and without a remediation hint. ``error_class``
    (v0.36.0 D1) groups failures into design buckets for the rendered
    diagnosis paragraph; default ``"missing_on_disk"`` keeps backward
    compat for envelopes built from pre-v0.36 code paths.
    """

    model_config = ConfigDict(extra="forbid")

    raw: str
    reason: str
    count: int
    suggestion: str = ""
    error_class: str = "missing_on_disk"


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

    # v0.32.0 Phase 1.1: top-N highlights of the prior_errors list,
    # sorted by recurrence count descending. The architect.md template
    # interpolates this into a `## PATH VALIDATION HISTORY` block so
    # the model sees the most-recurrent failures called out rather
    # than buried in a long context dump. Keeping it as a separate
    # field (rather than re-sorting prior_errors at render time) lets
    # tests assert on the rendered subset directly without coupling
    # to the architect's prompt templating.
    most_recent_failures: list[PriorError] = Field(default_factory=list)

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

    def render_rejection_history(self, *, attempt: int) -> str:
        """v0.32.0 Phase 1.1 / v0.36.0 D1: render the architect's PATH
        VALIDATION HISTORY block as one paragraph per design-class.

        Returns the empty string when no failures have been recorded so
        the ``{rejection_history}`` placeholder in ``architect.md``
        renders as nothing on the first attempt (no header, no
        whitespace turbulence).

        ``attempt`` is the human-facing 1-based architect attempt
        number — included verbatim in the header so the model sees
        which retry it's on.

        v0.36.0 D1: collapses the v0.32.0 "RECURRED N times" path
        bullet list into one design-class diagnosis paragraph + the
        list of paths under that class. Sibling failures of the same
        class (e.g. all `.md` deliverables) now render as a single
        actionable instruction rather than N redundant bullets.
        """
        if not self.most_recent_failures:
            return ""

        # Group by error_class, preserving first-seen order so the
        # rendered output is deterministic across runs.
        by_class: dict[str, list[PriorError]] = {}
        order: list[str] = []
        for entry in self.most_recent_failures:
            if entry.error_class not in by_class:
                by_class[entry.error_class] = []
                order.append(entry.error_class)
            by_class[entry.error_class].append(entry)

        lines: list[str] = [
            f"## PATH VALIDATION HISTORY (Retry Attempt {attempt})",
        ]
        for cls in order:
            lines.append("")
            lines.append(diagnosis_for_class(cls))
            lines.append("")
            lines.append(f"Paths in class `{cls}`:")
            for entry in by_class[cls]:
                suggestion_part = (
                    f"; suggestion: {entry.suggestion}" if entry.suggestion else ""
                )
                lines.append(
                    f"- {entry.raw} (RECURRED {entry.count}× "
                    f"reason: {entry.reason}{suggestion_part})"
                )
        return "\n".join(lines)

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
        # v0.32.0 Phase 1.1: ``most_recent_failures`` is rendered into
        # the architect's prompt via :meth:`render_rejection_history`
        # (interpolated as ``{rejection_history}`` at delegate time).
        # Stripping it from the wire dict avoids duplicating the same
        # data twice in the model's context window.
        payload.pop("most_recent_failures", None)
        return payload


__all__ = [
    "PriorError",
    "TypedRetryEnvelope",
    "diagnosis_for_class",
    "_DIAGNOSIS_TEMPLATES",
]
