"""v0.27 Phase 4 prep (Commit 6): TypedRetryEnvelope model contract.

Tests the new :class:`orchestrator.retry_envelope.TypedRetryEnvelope`
in isolation + end-to-end against the existing
:func:`orchestrator.plan_phase._build_retry_env` call site so the
v0.27 refactor cannot regress the wire JSON shape.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.path_validator import PathValidationError
from orchestrator.plan_phase import _build_retry_env
from orchestrator.retry_envelope import PriorError, TypedRetryEnvelope


# ---------------------------------------------------------------------------
# TypedRetryEnvelope itself.
# ---------------------------------------------------------------------------


def test_default_envelope_has_empty_strings_not_none() -> None:
    """Empty fields are ``""`` (not ``None``) on the model so the
    wire JSON is a uniform string shape. The ``path_error_*`` keys
    are stripped when empty to preserve v0.26.2's byte-compat:
    non-PathValidationError retries should not surface them in the
    architect's rendered CONTEXT block."""
    env = TypedRetryEnvelope()
    payload = env.as_context_dict()
    assert payload["prior_attempt"] == ""
    assert payload["parse_error"] == ""
    assert payload["hint"] == ""
    assert "path_error_raw" not in payload
    assert "path_error_reason" not in payload
    assert "path_error_suggestion" not in payload
    assert payload["prior_errors"] == []
    assert payload["dropped_entries"] == []


def test_path_error_fields_appear_when_set() -> None:
    """When a PathValidationError populates the typed fields, they
    survive the strip and reach the wire payload."""
    env = TypedRetryEnvelope(
        path_error_raw="notes",
        path_error_reason="missing_on_disk",
        path_error_suggestion="src/notes",
    )
    payload = env.as_context_dict()
    assert payload["path_error_raw"] == "notes"
    assert payload["path_error_reason"] == "missing_on_disk"
    assert payload["path_error_suggestion"] == "src/notes"


def test_prior_errors_serialise_as_dicts() -> None:
    """``prior_errors`` is a list of ``PriorError`` models; the wire
    JSON renders each as a plain dict."""
    env = TypedRetryEnvelope(
        prior_errors=[
            PriorError(raw="src/foo.py", reason="missing_on_disk", count=2),
            PriorError(raw="notes", reason="missing_on_disk", count=3),
        ],
    )
    payload = env.as_context_dict()
    # v0.32.0 Phase 1.1: PriorError gained an optional ``suggestion``
    # field (default empty string) so the architect can see remediation
    # hints alongside the raw + reason. Older entries that don't set
    # it round-trip with ``suggestion=""``.
    # v0.36.0 D1: PriorError gained ``error_class`` (default
    # ``"missing_on_disk"``).
    assert payload["prior_errors"] == [
        {
            "raw": "src/foo.py",
            "reason": "missing_on_disk",
            "count": 2,
            "suggestion": "",
            "error_class": "missing_on_disk",
        },
        {
            "raw": "notes",
            "reason": "missing_on_disk",
            "count": 3,
            "suggestion": "",
            "error_class": "missing_on_disk",
        },
    ]


def test_envelope_round_trips_through_json() -> None:
    """``as_context_dict`` must produce JSON-native values so the
    payload survives ledger / debug serialisation."""
    env = TypedRetryEnvelope(
        prior_attempt="# Plan: ...",
        parse_error="missing path",
        dropped_entries=["notes"],
        path_error_raw="notes",
        path_error_reason="missing_on_disk",
    )
    encoded = json.dumps(env.as_context_dict())
    decoded = json.loads(encoded)
    assert decoded["dropped_entries"] == ["notes"]
    assert decoded["path_error_raw"] == "notes"


def test_envelope_rejects_extra_fields() -> None:
    """``extra="forbid"`` so typo'd keys are caught at construction
    time rather than silently dropped to wire."""
    with pytest.raises(Exception):
        TypedRetryEnvelope(prior_attempt="", typo_field="oops")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# _build_retry_env wire-compatibility — the v0.26.2 context shape must
# survive the refactor byte-for-byte.
# ---------------------------------------------------------------------------


def _make_base_env() -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id="plan-architect",
        target_agent="architect",
        action="consult",
        context={"existing": "value"},
    )


def test_build_retry_env_preserves_base_context() -> None:
    """The original base envelope's context survives — only the retry
    fields are merged in."""
    base = _make_base_env()
    new = _build_retry_env(
        base,
        prior_plan_md="# prior body",
        exc=None,
        errors_seen={},
        dropped_entries=[],
    )
    assert new.context["existing"] == "value"
    assert new.context["prior_attempt"] == "# prior body"


def test_build_retry_env_truncates_prior_attempt_to_2000_chars() -> None:
    """Back-compat: the prior-attempt body is truncated at 2000 chars."""
    base = _make_base_env()
    long_body = "x" * 5000
    new = _build_retry_env(
        base,
        prior_plan_md=long_body,
        exc=None,
        errors_seen={},
        dropped_entries=[],
    )
    assert len(new.context["prior_attempt"]) == 2000


def test_build_retry_env_with_path_validation_error() -> None:
    """A :class:`PathValidationError` populates the typed
    ``path_error_*`` fields. ``path_error_suggestion`` is present
    even when empty (the triple is rendered as a unit)."""
    base = _make_base_env()
    exc = PathValidationError(
        raw="notes", reason="missing_on_disk", suggestion=None
    )
    new = _build_retry_env(
        base,
        prior_plan_md="prior",
        exc=exc,
        errors_seen={("notes", "missing_on_disk"): 2},
        dropped_entries=["prior_drop"],
    )
    ctx = new.context
    assert ctx["path_error_raw"] == "notes"
    assert ctx["path_error_reason"] == "missing_on_disk"
    assert ctx["path_error_suggestion"] == ""
    # v0.32.0 Phase 1.1: PriorError gained ``suggestion`` (default "").
    # v0.36.0 D1: also gained ``error_class`` (default
    # ``"missing_on_disk"``; the classifier picks this for a top-level
    # ``notes`` path that does not end in .md).
    assert ctx["prior_errors"] == [
        {
            "raw": "notes",
            "reason": "missing_on_disk",
            "count": 2,
            "suggestion": "",
            "error_class": "missing_on_disk",
        }
    ]
    assert ctx["dropped_entries"] == ["prior_drop"]
    assert "no spec found" not in ctx["parse_error"].lower()


def test_build_retry_env_with_generic_exception() -> None:
    """A non-PathValidationError exception goes to ``parse_error``
    while the typed ``path_error_*`` triple is stripped from the
    wire payload (preserving v0.26.2 byte-compat)."""
    base = _make_base_env()
    new = _build_retry_env(
        base,
        prior_plan_md="prior",
        exc=RuntimeError("kaboom"),
        errors_seen={},
        dropped_entries=[],
    )
    assert "kaboom" in new.context["parse_error"]
    assert "path_error_raw" not in new.context
    assert "path_error_reason" not in new.context
    assert "path_error_suggestion" not in new.context


def test_build_retry_env_returns_new_envelope_not_mutating_input() -> None:
    """The base envelope is not mutated — the function returns a fresh
    :class:`DelegationEnvelope`."""
    base = _make_base_env()
    base_context_before = dict(base.context)
    new = _build_retry_env(
        base,
        prior_plan_md="prior",
        exc=None,
        errors_seen={},
        dropped_entries=[],
    )
    assert base.context == base_context_before
    assert new is not base
    assert "prior_attempt" not in base.context


# ---------------------------------------------------------------------------
# v0.36.0 D1: rendered rejection history groups by error_class.
# ---------------------------------------------------------------------------


def test_render_rejection_history_collapses_md_class_to_single_diagnosis() -> None:
    """Three sibling `.md` rejections render as ONE diagnosis paragraph
    (the new_md_deliverable template) plus a list of paths — not three
    independent RECURRED bullets."""
    env = TypedRetryEnvelope(
        most_recent_failures=[
            PriorError(
                raw="notes/foo.md",
                reason="missing_on_disk",
                count=1,
                error_class="new_md_deliverable",
            ),
            PriorError(
                raw="notes/bar.md",
                reason="missing_on_disk",
                count=1,
                error_class="new_md_deliverable",
            ),
            PriorError(
                raw="notes/baz.md",
                reason="missing_on_disk",
                count=1,
                error_class="new_md_deliverable",
            ),
        ]
    )
    rendered = env.render_rejection_history(attempt=2)
    # Diagnosis paragraph appears once.
    assert rendered.count("Choose ONE") == 1
    # All three paths appear under the same class section.
    assert "notes/foo.md" in rendered
    assert "notes/bar.md" in rendered
    assert "notes/baz.md" in rendered
    assert "new_md_deliverable" in rendered


def test_rejection_diagnosis_includes_action_options() -> None:
    """The new_md_deliverable diagnosis mentions both action options
    (drop the deliverable, or tag with [new])."""
    from orchestrator.retry_envelope import diagnosis_for_class

    paragraph = diagnosis_for_class("new_md_deliverable")
    assert "drop the documentation deliverable" in paragraph
    assert "[new]" in paragraph


def test_render_rejection_history_classifies_missing_on_disk() -> None:
    """A path with a generic missing_on_disk class gets the default
    diagnosis paragraph."""
    env = TypedRetryEnvelope(
        most_recent_failures=[
            PriorError(
                raw="src/missing.py",
                reason="missing_on_disk",
                count=2,
                error_class="missing_on_disk",
            )
        ]
    )
    rendered = env.render_rejection_history(attempt=1)
    assert "missing_on_disk" in rendered
    assert "These paths do not exist on disk" in rendered
