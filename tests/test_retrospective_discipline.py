"""Smoke checks for the Phase 8 release-retrospective discipline.

These mirror the CI gate in `.github/workflows/release.yml` so the same
invariants can be exercised locally. The intent is to fail loudly the moment
someone deletes the template, the PR template, or the initial retrospective —
or ships a retrospective without populating the required Section 5.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TEMPLATE_PATH = REPO_ROOT / "docs" / "release-retrospective-template.md"
PR_TEMPLATE_PATH = REPO_ROOT / ".github" / "pull_request_template.md"
RETRO_DIR = REPO_ROOT / "docs" / "retrospectives"
INITIAL_RETRO_PATH = RETRO_DIR / "v0.31.1.md"


def _section_bullets(markdown: str, header_pattern: str) -> list[str]:
    """Return the `- ` bullet lines under the first matching `## ` header.

    Stops at the next `## ` header. Mirrors the awk + grep gate in CI.
    """
    lines = markdown.splitlines()
    in_section = False
    bullets: list[str] = []
    header_re = re.compile(header_pattern, re.IGNORECASE)
    for line in lines:
        if line.startswith("## "):
            if in_section:
                # We hit the next top-level section; stop collecting.
                break
            if header_re.search(line):
                in_section = True
                continue
        if in_section and line.startswith("- "):
            bullets.append(line)
    return bullets


def test_retrospective_template_exists_and_has_required_sections() -> None:
    assert TEMPLATE_PATH.exists(), f"missing {TEMPLATE_PATH}"
    body = TEMPLATE_PATH.read_text(encoding="utf-8")

    required_headers = [
        "# Release Retrospective:",
        "## 1. What shipped",
        "## 2. What broke in the field",
        "## 3. Per-fix verdict",
        "## 4. What's NEW",
        # The required Section 5 — title may include a leading emoji, so match loosely.
        "What's the NEXT layer of failure?",
        "## 6. Process / discipline notes",
        "## 7. Action items for v(X.Y.Z+1)",
    ]
    for header in required_headers:
        assert header in body, f"template missing required header fragment: {header!r}"

    # Section 5 must explicitly call itself out as REQUIRED so authors can't
    # quietly drop it.
    assert "REQUIRED" in body, "template must mark Section 5 as REQUIRED"


def test_pr_template_exists() -> None:
    assert PR_TEMPLATE_PATH.exists(), f"missing {PR_TEMPLATE_PATH}"
    body = PR_TEMPLATE_PATH.read_text(encoding="utf-8")
    # The release-bump checklist is the load-bearing part of this template;
    # if it's gone the gate is silently weakened.
    assert "Release-bump checklist" in body, "PR template missing release-bump checklist"
    assert "docs/retrospectives" in body, (
        "PR template must reference docs/retrospectives/<prior-version>.md"
    )
    assert "next layer of failure" in body.lower(), (
        "PR template release-bump checklist must reference the Section 5 requirement"
    )


def test_retrospective_directory_has_initial_entry() -> None:
    assert RETRO_DIR.exists() and RETRO_DIR.is_dir(), f"missing {RETRO_DIR}"
    assert INITIAL_RETRO_PATH.exists(), f"missing {INITIAL_RETRO_PATH}"

    body = INITIAL_RETRO_PATH.read_text(encoding="utf-8")

    # Section 5 header — match loosely (emoji optional, case-insensitive).
    bullets = _section_bullets(body, r"next layer of failure")
    assert len(bullets) >= 3, (
        f"{INITIAL_RETRO_PATH} 'Next layer of failure' section has "
        f"{len(bullets)} bullets; minimum 3 required (mirrors the CI gate)."
    )

    # Each bullet should look like a real candidate, not a placeholder.
    for bullet in bullets[:3]:
        assert "Severity" in bullet, (
            f"candidate bullet missing 'Severity:' field: {bullet!r}"
        )
        assert "Falsifiable signal" in bullet, (
            f"candidate bullet missing 'Falsifiable signal:' field: {bullet!r}"
        )
