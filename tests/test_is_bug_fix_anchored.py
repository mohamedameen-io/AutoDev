"""Anchored ``is_bug_fix`` gate (WS2-15).

DEFECT (pre-fix): ``is_bug_fix`` used an UNANCHORED substring match, so a feature
or refactor spec that merely *contains* a bug-word as a substring (e.g.
``bugfix-tracker``, ``debug-mode``, ``errorpage``, ``crashlytics``, ``failsafe``,
``wrongful``) was misclassified as a bug fix and routed down the bug-diagnosis
path. This gate pins the *engagement* property: feature/refactor specs that only
embed a bug-substring must be ``False``; real defect specs must be ``True``.

This is an ENGAGEMENT gate, not a vacuous one: ``test_red_substring_leak_is_fixed``
asserts on the SPECIFIC leaky cases that returned ``True`` under the old
substring match, and ``test_broken_control_substring_reintroduces_leak`` proves a
bare-substring matcher re-misclassifies them (so reverting the fix turns this
table red).
"""

from __future__ import annotations

import re

import pytest

from orchestrator.diagnosis_phase import _BUG_MARKERS, is_bug_fix

# --- the table -------------------------------------------------------------

# Feature / refactor / docs specs that EMBED a bug-word as a substring but are
# NOT defects. Each was misclassified True by the old unanchored matcher.
LEAKY_FEATURE_SPECS: tuple[str, ...] = (
    "Add a bugfix-tracker feature to the dashboard",
    "Implement a debug-mode toggle in settings",
    "Add a new errorpage template for 404s",
    "Refactor the crashlytics-uploader module",
    "Add a failsafe retry wrapper",
    "Implement a wrongful-termination report generator",
    "## Scope: Add a debugger panel to the dev tools",
)

# Plain feature specs with no bug substring at all (control: must stay False).
PLAIN_FEATURE_SPECS: tuple[str, ...] = (
    "Add a CSV export endpoint for the reports page",
    "Implement a CSV export endpoint for the reports page",
    "## Scope: Add a new dashboard widget to display per-user metrics",
    # feature that ADDS error handling — soft marker 'error' + feature-verb lead.
    "Add error handling to the upload flow",
    "Implement structured error reporting for the API",
)

# Genuine defect specs — must be True.
REAL_BUG_SPECS: tuple[str, ...] = (
    "Fix the off-by-one in add()",
    "NullPointerException when the list is empty",
    "There is a regression: the parser fails on empty input",
    "Bug: incorrect total when the cart is empty",
    "## Scope: Fix the crash that happens on startup",
    "The login button is broken on Safari",
    "App crashes when uploading a 0-byte file",
    "Traceback raised in the CSV parser on empty input",
    "ValueError thrown when parsing negative amounts",
)


@pytest.mark.parametrize("spec", LEAKY_FEATURE_SPECS)
def test_red_substring_leak_is_fixed(spec: str) -> None:
    """The leaky cases (True under substring match) must now be False."""
    assert is_bug_fix(spec) is False, f"feature spec misclassified as bug: {spec!r}"


@pytest.mark.parametrize("spec", PLAIN_FEATURE_SPECS)
def test_plain_features_are_not_bugs(spec: str) -> None:
    assert is_bug_fix(spec) is False, f"feature spec misclassified as bug: {spec!r}"


@pytest.mark.parametrize("spec", REAL_BUG_SPECS)
def test_real_bugs_are_detected(spec: str) -> None:
    assert is_bug_fix(spec) is True, f"real bug spec missed: {spec!r}"


@pytest.mark.parametrize("spec", ("", "   ", "\n\t  \n"))
def test_empty_is_not_a_bug(spec: str) -> None:
    assert is_bug_fix(spec) is False


def test_broken_control_substring_reintroduces_leak() -> None:
    """BROKEN-CONTROL: the OLD bare-substring matcher re-misclassifies the leaky
    feature specs (proves the table is non-vacuous and the fix is load-bearing).

    A gate that passes on the empty/found-nothing case is the bug: here we show
    the pre-fix algorithm flips the table red, so the green above is real.
    """

    def _substring_is_bug_fix(spec: str) -> bool:
        if not spec or not spec.strip():
            return False
        lower = spec.lower()
        return any(marker in lower for marker in _BUG_MARKERS)

    # The substring matcher MUST misclassify at least the leaky cases as bugs.
    misclassified = [s for s in LEAKY_FEATURE_SPECS if _substring_is_bug_fix(s)]
    assert misclassified == list(LEAKY_FEATURE_SPECS), (
        "broken control did not reproduce the substring leak; the gate would be "
        f"vacuous. Non-leaking under substring: "
        f"{[s for s in LEAKY_FEATURE_SPECS if s not in misclassified]}"
    )

    # And the ANCHORED matcher must NOT misclassify them (the fix is real).
    assert all(not is_bug_fix(s) for s in LEAKY_FEATURE_SPECS)


def test_anchored_markers_do_not_match_substrings() -> None:
    """Word-anchored markers must not fire inside larger tokens (the core fix)."""
    # 'bug' anchored should not match 'debug'/'bugfix'; but should match 'a bug'.
    word_re = re.compile(r"\bbug\b", re.IGNORECASE)
    assert word_re.search("debug the layout") is None
    assert word_re.search("a nasty bug here") is not None
