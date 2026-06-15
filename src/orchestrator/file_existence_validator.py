"""v0.24.3: enforce filesystem existence of architect-emitted file paths.

Released to close the bug class where the architect LLM emits a markdown
plan whose ``Files:`` lines reference paths that don't exist on disk —
previously the worker died mid-task with ``[Errno 63] File name too long``
because the path was a real directory prefix grafted onto literal source
content (``Pp.cpp`` and friends from a vendored 358K-file subtree).

The flow:

1. :func:`orchestrator.plan_parser.parse_plan_markdown` returns a structurally
   valid :class:`Plan` (text-only validation; no filesystem coupling).
2. :func:`validate_files_exist` walks every ``Task.files``,
   ``Task.extended_scope``, ``Phase.edit_scope``, and ``Plan.edit_scope``
   entry and confirms the path either:

   * exists as a tracked file in the repo (for ``Task.files``), OR
   * is a directory prefix under which at least one tracked file lives
     (for the ``*_scope`` lists), OR
   * is listed in ``Task.files_new`` — the v0.24.3 opt-out for paths the
     task itself will create, OR
   * (v0.40.1) exists on the real filesystem even when the git ls-files
     snapshot misses it. ``Task.files`` accepts an on-disk *file*; the
     ``*_scope`` lists accept an on-disk *file or directory*. This closes
     the false-rejection class where a gitignored or freshly-written path
     (e.g. ``.env.example``) or a file-shaped scope entry (e.g.
     ``pyproject.toml`` declared in ``edit_scope``) was rejected despite
     existing. The fallback only ever loosens acceptance.

3. On the first miss, :class:`PathValidationError` flows back into the
   existing v0.22.4 architect-retry envelope at
   :mod:`orchestrator.plan_phase` (lines 173-211). The caught exception
   carries ``raw`` (the offending path), ``reason="missing_on_disk"``,
   and ``suggestion`` (a difflib fuzzy match against the cached
   ``git ls-files`` snapshot), so the architect can self-correct on the
   second pass.

v0.25.0 upgrade: when ``.autodev/index.db`` exists, the fuzzy-match
suggestion path prefers ``IndexQuery.search_files`` (sqlite-FTS5 trigram
search) over the ``difflib`` fallback. Higher-quality suggestions, no
extra subprocess. The v0.24.3 fallback path remains as the no-index branch
so non-indexed contexts (test fixtures, brand-new ``init`` runs before
first build) still get a useful "did you mean" hint.

Why a separate module: keeps :mod:`plan_parser` pure-text (existing tests
stay green; ``parse_plan_markdown`` remains usable in fixtures without a
real filesystem) and keeps :mod:`path_validator` focused on string-shape
validation per its own docstring scope.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.path_validator import PathValidationError
from orchestrator.plan_parser import _normalize_path_entry
from state.schemas import Plan


def _hint_is_plausible(rejected: str, suggested: str) -> bool:
    """v0.36.0 D2: gate fuzzy "did you mean" suggestions.

    Returns ``False`` (suppress the hint) when:

    * The top-level directory component differs (e.g. ``notes`` vs
      ``.claude/skills/...``). Cross-tree suggestions almost always
      misdirect the architect — the correct path lives in the same
      subtree the rejection came from.
    * The rejected path is short (< 8 chars) AND the difflib similarity
      to the suggestion is < 0.85. Short paths produce noisy matches
      under the legacy 0.7 cutoff; this is the second filter on top of
      the closest()-level cutoff bump.
    """
    if not rejected or not suggested:
        return False
    rejected_top = rejected.split("/", 1)[0]
    suggested_top = suggested.split("/", 1)[0]
    if rejected_top != suggested_top:
        return False
    if len(rejected) < 8:
        similarity = difflib.SequenceMatcher(None, rejected, suggested).ratio()
        if similarity < 0.85:
            return False
    return True


def _classify_rejection(raw_path: str) -> str:
    """v0.36.0 D1: bucket a rejected path into a design-class string.

    Two classes today:

    * ``"new_md_deliverable"`` — the path looks like a brand-new
      documentation deliverable the architect wants to author. The
      retry envelope renders the action options for that class
      (drop the deliverable OR tag with ``[new]`` everywhere it's
      referenced).
    * ``"missing_on_disk"`` — generic missing-file rejection. Default
      class so additions to the rejection class catalogue don't
      retroactively reclassify older sites.

    The classifier is intentionally narrow — it recognises only the
    well-understood offender pattern from the v0.32.0 fixture (a new
    `.md` file under ``notes/`` or ``investigation/``). Other path
    shapes fall through to the generic class so the retry-envelope
    diagnosis stays accurate.
    """
    lower = raw_path.lower()
    if lower.endswith(".md"):
        # The classifier matches paths that look like the architect is
        # proposing a fresh investigation/notes deliverable (the v0.32.0
        # fixture's offender shape).
        if "notes" in lower or "investigation" in lower or lower.startswith(
            "notes/"
        ):
            return "new_md_deliverable"
    return "missing_on_disk"


class _RepoFileSnapshot:
    """Lazy, one-shot ``git ls-files`` cache.

    Used both for existence checks (``exists`` / ``is_dir_prefix``) and
    fuzzy-match suggestion (``closest``). The snapshot is built on first
    lookup and cached on the instance for the duration of one
    :func:`validate_files_exist` call — this keeps the subprocess cost
    bounded at one call per validation pass, not one per missing file.

    Subprocess invocation mirrors the pattern in
    :mod:`runtime.repo_probe` (``["git", "ls-files"]`` with
    ``capture_output=True``). On non-git trees or git failure the snapshot
    becomes the empty set; ``exists`` then returns ``False`` for everything
    and the caller decides whether to fail-soft or hard.

    v0.25.0: optionally accepts an ``IndexQuery`` instance. When supplied,
    ``closest()`` queries the index first (sqlite-FTS5 trigram match) and
    falls back to the v0.24.3 ``difflib`` path on miss. ``exists`` and
    ``is_dir_prefix`` remain on the git ls-files snapshot — those are
    cheap set lookups and the index doesn't add value there.
    """

    __slots__ = ("_cwd", "_tracked", "_loaded", "_index_query")

    def __init__(
        self, cwd: Path, *, index_query: Any | None = None
    ) -> None:
        self._cwd = cwd
        self._tracked: frozenset[str] = frozenset()
        self._loaded = False
        self._index_query = index_query

    @classmethod
    def for_cwd(
        cls, cwd: Path, *, index_query: Any | None = None
    ) -> "_RepoFileSnapshot":
        """Build a snapshot bound to *cwd*.

        Subprocess runs lazily on first lookup. Optional ``index_query``
        (an :class:`state.file_index.IndexQuery` instance) gives ``closest``
        a higher-quality suggestion source when the v0.25.0 index is
        available.
        """
        return cls(cwd, index_query=index_query)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            out = subprocess.run(
                ["git", "ls-files"],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if out.returncode != 0:
            return
        self._tracked = frozenset(
            line.strip() for line in out.stdout.splitlines() if line.strip()
        )

    @property
    def is_empty(self) -> bool:
        """True when the snapshot has no tracked files.

        Distinguishes "no validation possible" (non-git tree, git failure,
        or empty repo) from "snapshot loaded but path absent". The
        validator short-circuits on an empty snapshot — without ground
        truth we have no basis to flag any path as missing.
        """
        self._ensure_loaded()
        return len(self._tracked) == 0

    def exists(self, rel_path: str) -> bool:
        """Set membership against the tracked-file snapshot."""
        self._ensure_loaded()
        return rel_path in self._tracked

    def exists_on_disk(self, rel_path: str) -> bool:
        """True if ``cwd/rel_path`` exists on the real filesystem as a file.

        Additive fallback to :meth:`exists` for ``Task.files`` entries. The
        git ls-files snapshot misses two real-but-untracked populations the
        architect may legitimately reference: gitignored files
        (``.env.example``) and freshly-written-but-not-yet-committed files.
        Both exist on disk; rejecting them as ``missing_on_disk`` is a false
        rejection. This check loosens acceptance only — a path absent from
        BOTH the snapshot and the filesystem still falls through to the
        existing raise.

        ``is_file()`` (not ``exists()``) keeps the file/dir distinction the
        ``Task.files`` contract carries: ``files`` entries are files, not
        directory prefixes (that's ``extended_scope`` / ``edit_scope``).
        """
        if not rel_path:
            return False
        return (self._cwd / rel_path).is_file()

    def is_dir_prefix(self, rel_path: str) -> bool:
        """True if any tracked file path starts with ``rel_path + "/"``.

        Used for ``*_scope`` list entries which are repo-relative directory
        prefixes (not files). An empty / no-op scope is the caller's
        responsibility to short-circuit before calling here — we treat the
        empty string as "no prefix to check" and return ``False``.
        """
        if not rel_path:
            return False
        self._ensure_loaded()
        prefix = rel_path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._tracked)

    def scope_exists_on_disk(self, rel_path: str) -> bool:
        """True if ``cwd/rel_path`` exists on disk as a directory OR a file.

        Additive fallback to :meth:`is_dir_prefix` for the ``*_scope`` lists
        (``Plan.edit_scope``, ``Phase.edit_scope``, ``Task.extended_scope``).
        Two cases the tracked-prefix check alone misses:

        * A real directory that contains only untracked / gitignored files
          (so no tracked path carries the ``rel_path + "/"`` prefix) — the
          architect's intent to edit within it is still valid.
        * A FILE path declared in an ``*_scope`` list. The dir-prefix check
          structurally fails for a file (no tracked path starts with
          ``file + "/"``), yet a file entry is the architect declaring intent
          to edit that one existing file. Accepting it is correct.

        ``exists()`` (file or dir) is intentionally broad here: unlike
        ``Task.files``, scope entries legitimately name either shape.
        """
        if not rel_path:
            return False
        return (self._cwd / rel_path.rstrip("/")).exists()

    def closest(self, rel_path: str) -> str | None:
        """Return the closest tracked path.

        v0.25.0: prefer ``IndexQuery.search_files`` when an index is
        available — sqlite-FTS5 trigram matching beats difflib on
        substring/typo cases and avoids loading the entire ls-files
        snapshot into Python for the comparison.

        Fallback (no index, or index query returned no hits): the v0.24.3
        path — :func:`difflib.get_close_matches` over the cached
        ``git ls-files`` snapshot. ``cutoff=0.7`` filters hopelessly-
        different paths; v0.36.0 D2 bumps the cutoff to ``0.85`` for
        short paths (< 12 chars) where 0.7 produced too many spurious
        matches.

        v0.36.0 D2: the final candidate (from either source) is gated
        by :func:`_hint_is_plausible` — suggestions whose top-level
        directory differs from the rejected path are suppressed, since
        cross-directory hints are nearly always misleading.
        """
        # v0.25.0: index-first path. ``IndexQuery.search_files`` returns
        # a list of ``FileHit``; take the top hit's ``.path``. Wrap in a
        # broad except so a transient index error never blocks the
        # validator's primary job (raising PathValidationError on the
        # caller side).
        candidate: str | None = None
        if self._index_query is not None:
            try:
                hits = self._index_query.search_files(rel_path, limit=1)
                if hits:
                    candidate = hits[0].path
            except Exception:  # noqa: BLE001 - fall through to difflib
                candidate = None

        if candidate is None:
            self._ensure_loaded()
            if not self._tracked:
                return None
            # v0.36.0 D2: tighter cutoff for short paths. Short rejected
            # paths produce too many spurious matches at 0.7 — the
            # archived v0.32 incident was ``closest("notes")``
            # returning a path under ``.claude/skills/``.
            cutoff = 0.85 if len(rel_path) < 12 else 0.7
            matches = difflib.get_close_matches(
                rel_path, self._tracked, n=1, cutoff=cutoff
            )
            candidate = matches[0] if matches else None

        if candidate is None:
            return None
        if not _hint_is_plausible(rel_path, candidate):
            return None
        return candidate


def _collect_plan_new_files(plan: Plan) -> frozenset[str]:
    """v0.33.0 A1: union of every task's ``files_new`` across the plan.

    Each entry is canonicalised through :func:`_normalize_path_entry`
    (same code path the parser uses on the ``Files:`` line) so leading
    ``./`` segments and trailing slashes collapse to the same key the
    snapshot lookup uses. Drop reports with ``path is None`` are
    skipped — they would never have reached the validator anyway.
    """
    out: set[str] = set()
    for phase in plan.phases:
        for task in phase.tasks:
            for entry in task.files_new or ():
                report = _normalize_path_entry(entry)
                if report.path is not None:
                    out.add(report.path)
    return frozenset(out)


def _declaring_task_id(plan: Plan, path: str) -> str | None:
    """First task whose ``files_new`` canonicalises to *path*.

    Returns ``None`` if no task declares it (caller is responsible for
    only invoking this on paths known to be in the plan-global union).
    """
    for phase in plan.phases:
        for task in phase.tasks:
            for entry in task.files_new or ():
                report = _normalize_path_entry(entry)
                if report.path == path:
                    return task.id
    return None


def validate_files_exist(
    plan: Plan,
    cwd: Path,
    *,
    resolutions: list[dict[str, str]] | None = None,
) -> None:
    """Raise :class:`PathValidationError` on the first missing path.

    Walks, in order:

    * Each ``Task.files`` entry (existence as a tracked file).
    * Each ``Task.extended_scope`` entry (directory prefix — at least one
      tracked file under ``cwd/<prefix>/``).
    * Each ``Phase.edit_scope`` entry (same dir-prefix semantics).
    * ``Plan.edit_scope`` entries (same).

    Paths listed in ``Task.files_new`` are skipped during the
    ``Task.files`` walk — they are the architect's declared
    about-to-be-created files and have no on-disk presence yet.

    On the first miss::

        raise PathValidationError(
            raw=offending_path,
            reason="missing_on_disk",
            suggestion=snapshot.closest(offending_path),
        )

    The caller (``orchestrator.plan_phase.run_plan_phase``) catches this
    exception in the existing v0.22.4 retry envelope and feeds the
    structured error back to the architect for a corrected second pass.

    No-op short-circuit: when ``cwd`` is not a git tree (or git ls-files
    yields nothing), the snapshot is empty and we have no basis to flag
    paths as missing. We skip silently in that case — non-git contexts
    (test fixtures, scratch dirs, brand-new ``git init`` with no files)
    aren't the population this validator was built for.

    v0.25.0: when ``.autodev/index.db`` exists, the snapshot's ``closest``
    method queries it for higher-quality fuzzy suggestions; otherwise the
    v0.24.3 difflib-over-ls-files path runs.

    v0.33.0 A1: ``Task.files`` membership also passes when the path is
    declared ``[new]`` by ANY task in the plan (not just the current
    task). The plan-global union is computed once and consulted as a
    fallback after the per-task ``files_new`` check. The validator is
    sync but ledger writes are async — when *resolutions* is provided,
    each plan-global admission is appended to that list and the caller
    is responsible for ledger emission. The
    ``path_validation_resolved_via_plan_global`` op encodes the
    ``{task_id, path, declaring_task_id}`` payload.

    v0.40.1: additive real-filesystem fallback. The git ls-files snapshot
    is a *tracked* view; it misses gitignored files, freshly-written
    uncommitted files, and dirs holding only untracked files. Each check
    site now also accepts a path that actually exists on disk:
    ``Task.files`` via :meth:`_RepoFileSnapshot.exists_on_disk` (file only),
    and the ``*_scope`` lists via
    :meth:`_RepoFileSnapshot.scope_exists_on_disk` (file or directory). An
    on-disk ``Task.files`` admission is a *present* file, not a ``[new]``
    one, so it does NOT append to *resolutions*. The fallback is purely
    additive — a path missing from BOTH the snapshot AND the filesystem
    still raises ``PathValidationError`` with the unchanged shape.
    """
    # v0.25.0: try to wire the IndexQuery for richer suggestions. Best-effort:
    # if the index module isn't importable (e.g. parallel agent's code not
    # landed yet) or the db doesn't exist, fall through to the v0.24.3
    # git-ls-files-only path.
    index_query: Any | None = None
    db_path = cwd / ".autodev" / "index.db"
    if db_path.exists():
        try:
            from state.file_index import IndexQuery

            index_query = IndexQuery(db_path)
        except Exception:  # noqa: BLE001 - graceful no-index fallback
            index_query = None

    snapshot = _RepoFileSnapshot.for_cwd(cwd, index_query=index_query)
    if snapshot.is_empty:
        return

    plan_global_new = _collect_plan_new_files(plan)

    # Plan-level edit_scope: dir-prefix check, with on-disk fallback
    # (a real dir with only untracked files, or a file path the architect
    # declares intent to edit).
    for entry in plan.edit_scope:
        if snapshot.is_dir_prefix(entry):
            continue
        if snapshot.scope_exists_on_disk(entry):
            continue
        raise PathValidationError(
            raw=entry,
            reason="missing_on_disk",
            suggestion=snapshot.closest(entry),
            error_class=_classify_rejection(entry),
        )

    for phase in plan.phases:
        # Phase-level edit_scope override (None = inherit, list = explicit).
        if phase.edit_scope is not None:
            for entry in phase.edit_scope:
                if snapshot.is_dir_prefix(entry):
                    continue
                if snapshot.scope_exists_on_disk(entry):
                    continue
                raise PathValidationError(
                    raw=entry,
                    reason="missing_on_disk",
                    suggestion=snapshot.closest(entry),
                    error_class=_classify_rejection(entry),
                )

        for task in phase.tasks:
            new_files: frozenset[str] = frozenset(task.files_new or [])

            for entry in task.files:
                if entry in new_files:
                    continue
                if snapshot.exists(entry):
                    continue
                # On-disk fallback: the file exists on the real filesystem
                # but is absent from the git ls-files snapshot (gitignored,
                # or written-but-uncommitted). It is present, not "new", so
                # it does NOT flow through the plan-global resolutions ledger.
                if snapshot.exists_on_disk(entry):
                    continue
                # v0.33.0 A1: plan-global ``[new]`` admission. A sibling
                # task declaring this path with ``[new]`` is enough — the
                # earlier task will produce the file before this one runs.
                # Normalise both sides through the parser helper so e.g.
                # ``./foo.md`` matches ``foo.md``.
                report = _normalize_path_entry(entry)
                canonical = report.path
                if canonical is not None and canonical in plan_global_new:
                    if resolutions is not None:
                        resolutions.append(
                            {
                                "task_id": task.id,
                                "path": canonical,
                                "declaring_task_id": _declaring_task_id(
                                    plan, canonical
                                )
                                or "",
                            }
                        )
                    continue
                raise PathValidationError(
                    raw=entry,
                    reason="missing_on_disk",
                    suggestion=snapshot.closest(entry),
                    error_class=_classify_rejection(entry),
                )

            for entry in task.extended_scope:
                if snapshot.is_dir_prefix(entry):
                    continue
                if snapshot.scope_exists_on_disk(entry):
                    continue
                raise PathValidationError(
                    raw=entry,
                    reason="missing_on_disk",
                    suggestion=snapshot.closest(entry),
                    error_class=_classify_rejection(entry),
                )


__all__ = ["validate_files_exist"]
