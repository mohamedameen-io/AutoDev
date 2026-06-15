"""Debug-tag cleanup gate (ADR-0046, Phase 6).

Blocks a task when leftover debugging markers — e.g. ``[DEBUG-XYZ]`` print
statements / log lines inserted during diagnosis — remain in the changed
files. The diagnose discipline instruments the code to confirm a cause, then
*removes* that instrumentation in the cleanup phase; this gate is the
machine-checkable backstop for "did we actually clean up".

It deliberately **reuses the secret-scan scanning machinery**
(:func:`qa.secretscan._iter_files` for the changed-files / whole-tree walk and
oversized-path resilience, plus :func:`qa.secretscan._path_in_scope` /
:func:`qa.secretscan._matches_allowlist` for scope and allowlist filtering)
rather than re-implementing the file walk. The only thing specialised here is
the tag pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

from plugins.registry import GateResult
from qa._io import safe_read_source
from qa.secretscan import (
    _iter_files,
    _load_allowlist,
    _matches_allowlist,
    _path_in_scope,
)


# Default marker family. Matches ``[DEBUG-...]`` style tags an agent emits
# while instrumenting (``[DEBUG-1]``, ``[DEBUG-TRACE]``, ``[DEBUG-AUTH-FLOW]``).
# Case-insensitive so ``[debug-x]`` is caught too. The trailing ``-`` after
# ``DEBUG`` is what distinguishes an intentional leftover marker from prose
# that merely contains the word "debug".
_DEFAULT_DEBUG_TAG = re.compile(r"\[DEBUG-[^\]]*\]", re.IGNORECASE)

# Cap the number of findings rendered in the detail string (mirrors
# secretscan's 20-line truncation) so a pathological file does not produce a
# multi-megabyte gate detail.
_MAX_REPORTED = 20


async def run_debug_tag_gate(
    cwd: Path,
    paths: list[Path] | None = None,
    edit_scope: list[str] | None = None,
    *,
    pattern: str | re.Pattern[str] | None = None,
    ignore_paths: list[str] | None = None,
) -> GateResult:
    """Scan *cwd* for leftover debug tags and block when any remain.

    Parameters mirror :func:`qa.secretscan.run_secretscan` so the execute
    dispatcher can wire this gate with the same ``cwd`` / ``paths`` /
    ``edit_scope`` it already threads to the other diff-scoped gates.

    * ``paths`` — repo-relative changed files (the executor's diff). When
      ``None`` the whole tree is walked (legacy); when an empty list the gate
      short-circuits to a clean pass ("nothing to scan").
    * ``edit_scope`` — optional prefix filter composed with ``paths`` (only
      files in the intersection are scanned).
    * ``pattern`` — override the default ``[DEBUG-...]`` marker. Accepts a raw
      string (compiled case-insensitively) or a pre-compiled pattern.
    * ``ignore_paths`` — gitignore-style globs to skip (composes with the
      ``.autodev/secretscan-allow`` allowlist).

    Returns ``GateResult(passed=False, severity="block", ...)`` when any tag
    is found, ``GateResult(passed=True, ...)`` otherwise. Never raises — a
    pathological path or unreadable file is skipped, mirroring the secret-scan
    gate's resilience.
    """
    # Explicit no-op when the caller narrowed the diff scope to an empty list.
    # Distinct from ``paths=None`` (legacy whole-tree walk). Mirrors
    # :func:`qa.secretscan.run_secretscan`'s guard so the ledger records
    # "nothing to scan" rather than "scanned everything and passed".
    if paths is not None and not paths:
        return GateResult(
            passed=True,
            severity="info",
            details="debug_tag: no files in diff scope",
            metrics={},
        )

    if pattern is None:
        tag_re = _DEFAULT_DEBUG_TAG
    elif isinstance(pattern, re.Pattern):
        tag_re = pattern
    else:
        tag_re = re.compile(pattern, re.IGNORECASE)

    scope_active = bool(edit_scope)
    scope_prefixes = [p.rstrip("/") for p in (edit_scope or [])]

    allowlist = _load_allowlist(cwd)
    ignore_globs = list(ignore_paths or [])

    findings: list[str] = []
    files_with_tags = 0

    for path in _iter_files(cwd, paths=paths):
        try:
            rel_for_scope = path.relative_to(cwd).as_posix()
        except ValueError:
            # Absolute path outside cwd: can't decide scope membership when a
            # scope filter is active — treat as out-of-scope (conservative).
            if scope_active:
                continue
            rel_for_scope = path.as_posix()

        if scope_active and not _path_in_scope(rel_for_scope, scope_prefixes):
            continue
        if allowlist and _matches_allowlist(rel_for_scope, allowlist):
            continue
        if ignore_globs and _matches_allowlist(rel_for_scope, ignore_globs):
            continue

        content = safe_read_source(path)
        if content is None:
            continue

        try:
            rel = path.relative_to(cwd).as_posix()
        except ValueError:
            rel = path.as_posix()

        matched_in_file = False
        for lineno, line in enumerate(content.splitlines(), start=1):
            for match in tag_re.finditer(line):
                matched_in_file = True
                findings.append(f"{rel}:{lineno}: {match.group(0)}")
        if matched_in_file:
            files_with_tags += 1

    metrics: dict[str, object] = {
        "debug_tags_found": len(findings),
        "files_with_debug_tags": files_with_tags,
    }

    if findings:
        detail = "leftover debug tags found (remove before merge):\n" + "\n".join(
            findings[:_MAX_REPORTED]
        )
        if len(findings) > _MAX_REPORTED:
            detail += f"\n… and {len(findings) - _MAX_REPORTED} more"
        return GateResult(
            passed=False,
            severity="block",
            details=detail,
            metrics=metrics,
        )

    return GateResult(
        passed=True,
        details="no leftover debug tags",
        metrics=metrics,
    )


__all__ = ["run_debug_tag_gate"]
