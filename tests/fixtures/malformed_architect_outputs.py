"""Hedge-text patterns the v0.27 parser/validator chain must reject upstream.

Each fixture is a self-contained plan-markdown snippet exercising one
class of architect-output malformation. Twelve patterns total — eleven
exercise a bug class, one (``LEGITIMATE_SPACE_WITH_SLASH_REGRESSION``)
is a regression guard ensuring a legitimate path is NOT rejected.

All fixtures use generic synthetic paths (``src/math/__init__.py``,
``src/foo.cpp``, ``docs/My File.md``). No proprietary project content.

Each entry is a ``HedgeFixture`` with three fields:

  * ``markdown``: the architect-output plan-markdown to feed the parser.
  * ``v026_behavior``: a label for what the v0.26.2 parser does today
    (e.g. ``parse_ok_validate_fail`` — parses, but validate_files_exist
    rejects). Lets the Phase 0 parametrised test pin the baseline.
  * ``phase_1_target``: the target behavior after Phase 1 lands —
    typically ``parser_drops_then_validate_ok`` (the parser strips the
    hedge upstream and validation passes on the surviving entries).

The Phase 0 parametrised test (``test_orchestrator_plan_parser_hedge_text.py``)
documents the v0.26.2 baseline; Commit 3 (Phase 1 parser hardening) is
expected to flip several fixtures' realised behavior to the
``phase_1_target`` label.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


HedgeBehavior = Literal[
    # Parser raises PlanParseError.
    "parse_error",
    # Parser succeeds but the on-disk validator (validate_files_exist /
    # PathValidationError) rejects one or more entries.
    "parse_ok_validate_fail",
    # Parser silently strips/normalises the hedge and validation passes.
    # Target state for hardened entries after Phase 1 lands.
    "parse_drops_then_validate_ok",
    # Parser+validator both accept (regression-guard fixtures).
    "parse_ok_validate_ok",
]


@dataclass(frozen=True)
class HedgeFixture:
    """One synthetic architect-output hedge-text pattern."""

    name: str
    bug_class: str
    markdown: str
    v026_behavior: HedgeBehavior
    phase_1_target: HedgeBehavior


# 1. Parens-hedged Files entry: ``a.py, (and any helper file ...)``.
#    Comma-split keeps the paren entry; validator rejects "(and any ..."
#    because the path doesn't exist on disk.
PARENS_IN_FILES_LIST = HedgeFixture(
    name="parens_in_files_list",
    bug_class="paren-hedged Files entry",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract and any helper.
  - Files: src/math/__init__.py, (and any helper file identified during implementation)
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_drops_then_validate_ok",
)


# 2. ``[new]``-prefixed entry with a paren-hedge after the path.
#    v0.24.3 strips the ``[new]`` prefix and routes to files_new, which
#    ``validate_files_exist`` skips — so v0.26.2 silently accepts the
#    bogus path. The bug surfaces later when the developer attempts to
#    create ``src/math/helpers.py (if helpers are needed)``. Phase 1's
#    shape-check is expected to strip the paren-hedge upstream.
NEW_WITH_TRAILING_HEDGE = HedgeFixture(
    name="new_with_trailing_hedge",
    bug_class="[new] prefix with paren-hedge tail",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Create a new helper module.
  - Files: src/math/__init__.py, [new] src/math/helpers.py (if helpers are needed)
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_ok",
    phase_1_target="parse_drops_then_validate_ok",
)


# 3. Inline ``# comment`` on a Files entry.
#    Parser includes the comment as part of the path; validator rejects.
COMMENT_IN_FILES = HedgeFixture(
    name="comment_in_files",
    bug_class="inline # comment in Files",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py # main entry point
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_drops_then_validate_ok",
)


# 4. Bare-token EDIT_SCOPE entry that isn't a real path.
#    The classic v0.26.2 drop case ("notes").
NOTES_BARE_TOKEN_EDIT_SCOPE = HedgeFixture(
    name="notes_bare_token_edit_scope",
    bug_class="bare-token EDIT_SCOPE entry",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math
  - notes

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_ok_validate_fail",
)


# 5. Space-without-slash path: ``my notes``. Looks like a bare-token
#    hedge but the architect intended a single word "my notes" — the
#    parser cannot disambiguate "my notes" from "my" + "notes".
SPACE_WITHOUT_SLASH = HedgeFixture(
    name="space_without_slash",
    bug_class="space-only path (no /)",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py, my notes
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_drops_then_validate_ok",
)


# 6. Trailing comma on Files entry.
#    Comma-split produces an empty trailing entry; parser drops it.
TRAILING_COMMA = HedgeFixture(
    name="trailing_comma",
    bug_class="trailing comma",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py,
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_ok",
    phase_1_target="parse_ok_validate_ok",
)


# 7. Empty entry inside the list: ``a, , b``.
EMPTY_ENTRY = HedgeFixture(
    name="empty_entry",
    bug_class="empty entry in comma list",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py, , src/math/helpers.py
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_drops_then_validate_ok",
)


# 8. Unmatched bracket: ``[new src/math/helpers.py``.
#    The ``[new]`` regex requires a closing bracket; an unmatched bracket
#    falls through and the path is preserved as a regular entry.
UNMATCHED_BRACKET = HedgeFixture(
    name="unmatched_bracket",
    bug_class="unmatched [ bracket",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py, [new src/math/helpers.py
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_drops_then_validate_ok",
)


# 9. Non-ASCII characters in path: ``src/mäth/__init__.py``.
#    Should validate-fail because the on-disk file uses plain ASCII.
NON_ASCII_PATH = HedgeFixture(
    name="non_ascii_path",
    bug_class="non-ASCII characters in path",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/mäth/__init__.py
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_ok_validate_fail",
)


# 10. TBD placeholder as a path: ``TBD`` or ``TODO``.
TBD_PLACEHOLDER = HedgeFixture(
    name="tbd_placeholder",
    bug_class="TBD/TODO placeholder",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract.
  - Files: src/math/__init__.py, TBD
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_drops_then_validate_ok",
)


# 11. Forward-reference: a file that the next task will create but
#     hasn't yet existed. Should be tagged ``[new]`` per v0.24.3 but the
#     architect emitted it as a plain path.
FORWARD_REFERENCE = HedgeFixture(
    name="forward_reference",
    bug_class="forward-ref path without [new] prefix",
    markdown="""# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract; helpers come in 1.2.
  - Files: src/math/__init__.py, src/math/future_helpers.py
  - Acceptance:
    - [ ] subtract function exported
""",
    v026_behavior="parse_ok_validate_fail",
    phase_1_target="parse_ok_validate_fail",
)


# 12. REGRESSION GUARD: a legitimate space-containing path WITH a slash
#     (``docs/My File.md``). MUST validate cleanly — the Phase 1 shape
#     check must NOT reject paths just because they contain spaces.
LEGITIMATE_SPACE_WITH_SLASH_REGRESSION = HedgeFixture(
    name="legitimate_space_with_slash_regression",
    bug_class="REGRESSION GUARD — legitimate path with space + slash",
    markdown="""# Plan: Update documentation

EDIT_SCOPE:
  - docs

## Phase 1: Update doc

### Task 1.1: Update README
  - Description: Update a doc file with a space in the name.
  - Files: docs/My File.md
  - Acceptance:
    - [ ] doc updated
""",
    v026_behavior="parse_ok_validate_ok",
    phase_1_target="parse_ok_validate_ok",
)


ALL_HEDGE_FIXTURES: list[HedgeFixture] = [
    PARENS_IN_FILES_LIST,
    NEW_WITH_TRAILING_HEDGE,
    COMMENT_IN_FILES,
    NOTES_BARE_TOKEN_EDIT_SCOPE,
    SPACE_WITHOUT_SLASH,
    TRAILING_COMMA,
    EMPTY_ENTRY,
    UNMATCHED_BRACKET,
    NON_ASCII_PATH,
    TBD_PLACEHOLDER,
    FORWARD_REFERENCE,
    LEGITIMATE_SPACE_WITH_SLASH_REGRESSION,
]
