"""Secret-scan gate.

Scans the project for hard-coded secrets using regex patterns and
Shannon-entropy heuristics. Returns a :class:`~plugins.registry.GateResult`.

This gate is intentionally conservative: it reports findings as failures so
that secrets are caught before they reach a reviewer or are committed.
"""

from __future__ import annotations

import fnmatch
import math
import re
from pathlib import Path

from plugins.registry import GateResult


# ---------------------------------------------------------------------------
# Regex patterns for well-known secret formats
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub PAT", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub OAuth", re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("GitHub Actions token", re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("Private key header", re.compile(r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("Stripe secret key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("Generic API key assignment", re.compile(r'(?i)(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token)\s*[=:]\s*["\']?[A-Za-z0-9/+_\-]{20,}["\']?')),
]

# Files / directories to skip.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".tox",
})
_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".whl",
    ".lock",  # lock files contain hashes, not secrets
})

# Default entropy threshold for high-entropy string detection.
_ENTROPY_THRESHOLD = 4.5
# v0.19.0: per-extension entropy curves. Files with these extensions use a
# higher threshold to suppress legitimate high-entropy identifiers (Unity-class
# C++ codebases routinely use long camelCase symbols; YAML build manifests use
# base64-flavored hashes).
_DEFAULT_PER_EXTENSION_ENTROPY: dict[str, float] = {
    ".cpp": 5.0,
    ".cc": 5.0,
    ".cxx": 5.0,
    ".c": 5.0,
    ".h": 5.0,
    ".hpp": 5.0,
    ".hxx": 5.0,
    ".yaml": 5.5,
    ".yml": 5.5,
}
_MIN_ENTROPY_LEN = 20


def _path_in_scope(rel_path: str, scope_prefixes: list[str]) -> bool:
    """Return True iff *rel_path* lies under any prefix in *scope_prefixes*.

    Mirrors :func:`orchestrator.dag.is_in_scope` semantics — kept local
    here to avoid an orchestrator → qa import edge that would tangle
    layering. Empty / falsy *scope_prefixes* would be a programming
    error at this call site (the caller short-circuits before reaching
    here), but for safety we still return True in that case.
    """
    if not scope_prefixes:
        return True
    for raw_prefix in scope_prefixes:
        prefix = raw_prefix.rstrip("/")
        if rel_path == prefix:
            return True
        if rel_path.startswith(prefix + "/"):
            return True
    return False


def _shannon_entropy(text: str) -> float:
    """Compute Shannon entropy (bits per character) of *text*."""
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


def _entropy_threshold_for(
    extension: str | None,
    per_extension_thresholds: dict[str, float] | None,
) -> float:
    """Resolve entropy threshold for a file extension.

    Lookup order (first hit wins):
      1. Caller-supplied *per_extension_thresholds*.
      2. Module default :data:`_DEFAULT_PER_EXTENSION_ENTROPY`.
      3. Global :data:`_ENTROPY_THRESHOLD`.
    """
    if extension is None:
        return _ENTROPY_THRESHOLD
    if per_extension_thresholds and extension in per_extension_thresholds:
        return per_extension_thresholds[extension]
    if extension in _DEFAULT_PER_EXTENSION_ENTROPY:
        return _DEFAULT_PER_EXTENSION_ENTROPY[extension]
    return _ENTROPY_THRESHOLD


def _high_entropy_strings(
    content: str,
    extension: str | None = None,
    per_extension_thresholds: dict[str, float] | None = None,
    *,
    min_length: int | None = None,
    global_threshold_override: float | None = None,
) -> list[str]:
    """Return substrings that look like high-entropy secrets.

    *extension* is the lowercased file extension (including leading dot),
    used to look up a per-extension threshold. *per_extension_thresholds*
    is an optional caller override; it composes with the module defaults.

    v0.23.0 C2:
    * *min_length* overrides the legacy 20-char minimum (use 32 to filter
      Unity asset GUIDs without losing real keys).
    * *global_threshold_override* overrides :data:`_ENTROPY_THRESHOLD`
      (use 4.8 to suppress GUID-like 4.5-entropy candidates).
    """
    threshold = _entropy_threshold_for(extension, per_extension_thresholds)
    if global_threshold_override is not None and (
        extension is None
        or extension not in _DEFAULT_PER_EXTENSION_ENTROPY
        and (
            per_extension_thresholds is None
            or extension not in per_extension_thresholds
        )
    ):
        threshold = global_threshold_override
    effective_min_len = (
        int(min_length) if min_length is not None else _MIN_ENTROPY_LEN
    )
    pattern = (
        r'["\']([A-Za-z0-9/+_\-=]{' + str(effective_min_len) + r',})["\']'
    )
    candidates = re.findall(pattern, content)
    return [c for c in candidates if _shannon_entropy(c) >= threshold]


def _load_allowlist(cwd: Path) -> list[str]:
    """Read ``.autodev/secretscan-allow`` patterns (gitignore-style globs).

    Comment lines (``#`` prefix) and blank lines are ignored. Returns an
    empty list when the file is missing.
    """
    path = cwd / ".autodev" / "secretscan-allow"
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def _matches_allowlist(rel_path: str, patterns: list[str]) -> bool:
    """True iff *rel_path* matches any gitignore-style glob in *patterns*."""
    for raw in patterns:
        pat = raw.rstrip("/")
        # ``**`` recursive wildcard semantic: ``a/**`` matches ``a/b``, ``a/b/c``.
        if pat.endswith("/**"):
            head = pat[: -len("/**")]
            if rel_path == head or rel_path.startswith(head + "/"):
                return True
            continue
        if "**" in pat:
            # Fall back to fnmatch on the pattern (lossy but standard).
            if fnmatch.fnmatch(rel_path, pat.replace("**", "*")):
                return True
            continue
        if fnmatch.fnmatch(rel_path, pat):
            return True
        # Bare-prefix match: ``foo`` matches ``foo`` and ``foo/bar``.
        if rel_path == pat or rel_path.startswith(pat + "/"):
            return True
    return False


async def run_secretscan(
    cwd: Path,
    paths: list[Path] | None = None,
    edit_scope: list[str] | None = None,
    per_extension_thresholds: dict[str, float] | None = None,
    baseline_enabled: bool = False,
    *,
    ignore_paths: list[str] | None = None,
    entropy_threshold_override: float | None = None,
    min_entropy_length: int | None = None,
) -> GateResult:
    """Scan *cwd* for hard-coded secrets.

    v0.13.0: when *paths* is non-None, restrict the scan to the listed
    files (resolved relative to *cwd*). Non-existent paths are silently
    skipped. Files in the legacy ``_SKIP_EXTENSIONS`` set are still
    skipped even when explicitly listed (binary blobs are never useful
    secret carriers).

    When *paths* is None (legacy), walk the whole tree under *cwd*.

    v0.14.0: when *edit_scope* is non-empty, additionally restrict the
    scan to files under any prefix in the scope. Composes with *paths*:
    when both are set, only files in the intersection (in-diff AND
    in-scope) are scanned. When *paths* is None and *edit_scope* is
    non-empty, the whole-tree walk is filtered to the scope.

    v0.19.0: ``.autodev/secretscan-allow`` (gitignore-style globs) skips
    matching files. ``per_extension_thresholds`` overrides the entropy
    threshold per file extension; defaults to the module-level table that
    raises the threshold for ``.cpp/.h/.yaml``.

    Returns ``GateResult(passed=False, ...)`` if any secrets are found,
    ``GateResult(passed=True, ...)`` otherwise.
    """
    findings: list[str] = []

    # Normalize the scope to "filter is active" iff non-empty list. Empty
    # list / None preserves legacy semantics (no scope filter).
    scope_active = bool(edit_scope)
    scope_prefixes = [p.rstrip("/") for p in (edit_scope or [])]

    allowlist = _load_allowlist(cwd)
    # v0.23.0 C2: config-driven ignore_paths compose with .autodev/secretscan-allow
    # (same gitignore-style glob syntax). Operators on huge repos with test-fixture
    # density use this to skip e.g. ``**/Tests/**``, ``**/*.unity.meta``.
    ignore_globs = list(ignore_paths or [])

    for path in _iter_files(cwd, paths=paths):
        # v0.14.0 scope filter: applied after _iter_files so it composes
        # naturally with both walk modes (full-tree and diff-paths).
        try:
            rel_for_scope = path.relative_to(cwd).as_posix()
        except ValueError:
            # Caller passed an absolute path outside cwd. Without a
            # repo-relative form we can't decide scope membership;
            # treat as out-of-scope (conservative).
            if scope_active:
                continue
            rel_for_scope = path.as_posix()

        if scope_active and not _path_in_scope(rel_for_scope, scope_prefixes):
            continue

        # v0.19.0 allowlist filter.
        if allowlist and _matches_allowlist(rel_for_scope, allowlist):
            continue

        # v0.23.0 C2 cfg-driven ignore_paths filter (compose with allowlist).
        if ignore_globs and _matches_allowlist(rel_for_scope, ignore_globs):
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            rel = path.relative_to(cwd)
        except ValueError:
            # Caller passed an absolute path outside cwd. Use the path
            # as-is for reporting; never let this crash the gate.
            rel = path

        # Regex pattern scan.
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                findings.append(f"{rel}: {label}")

        # Entropy scan — per-extension threshold lookup.
        # v0.23.0 C2: when entropy_threshold_override is supplied (operator
        # tightening for huge-repo huge-fixture noise), pre-merge it into
        # the per-extension dict so it wins for ALL extensions that don't
        # have an explicit override of their own.
        effective_per_ext = dict(per_extension_thresholds or {})
        if entropy_threshold_override is not None:
            for _ext_default in (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
                                 ".hxx", ".yaml", ".yml"):
                effective_per_ext.setdefault(_ext_default, entropy_threshold_override)
        ext = path.suffix.lower()
        for suspect in _high_entropy_strings(
            content,
            extension=ext,
            per_extension_thresholds=effective_per_ext or None,
            min_length=min_entropy_length,
            global_threshold_override=entropy_threshold_override,
        ):
            findings.append(
                f"{rel}: high-entropy string ({_shannon_entropy(suspect):.2f} bits) — {suspect[:8]}…"
            )

    # v0.19.0: per-repo baseline filter — drop findings already accepted.
    if baseline_enabled and findings:
        from qa.secretscan_baseline import filter_against_baseline

        findings = await filter_against_baseline(findings, cwd)

    if findings:
        detail = "potential secrets found:\n" + "\n".join(findings[:20])
        if len(findings) > 20:
            detail += f"\n… and {len(findings) - 20} more"
        return GateResult(passed=False, details=detail)
    return GateResult(passed=True, details="no secrets detected")


def _iter_files(cwd: Path, paths: list[Path] | None = None):
    """Yield scannable files under *cwd*.

    Two modes:

    * ``paths=None`` (legacy): recursive ``cwd.rglob("*")`` walk, skipping
      known noise directories (``.git``, ``.venv``, ``node_modules``…) and
      file extensions in ``_SKIP_EXTENSIONS``.
    * ``paths=[...]`` (v0.13.0 diff-scope): yield only the files in the
      list. Each entry is resolved relative to *cwd* if not absolute.
      Non-existent paths and ``_SKIP_EXTENSIONS`` matches are skipped
      silently. Skip-dir filter is *not* applied — caller is expected to
      have curated the list (typically from a developer's diff, which is
      already scoped to changes the executor introduced).
    """
    if paths is None:
        for item in cwd.rglob("*"):
            if not item.is_file():
                continue
            # Skip noise directories.
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if item.suffix in _SKIP_EXTENSIONS:
                continue
            yield item
        return

    seen: set[Path] = set()
    for raw in paths:
        if not raw.is_absolute():
            candidate = cwd / raw
        else:
            candidate = raw
        # Bug #3 (v0.25.1): ``resolve()`` raises ``ValueError`` on embedded
        # NUL bytes (in addition to the existing ``OSError`` cases). Both
        # mean "not a real path" — treat as skip.
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        # Bug #3 (v0.25.1): wrap ``is_file()`` in ``OSError`` guard. A 4000-char
        # multi-line "path" extracted from a malformed diff resolves cleanly
        # (pure-string op) but trips ``os.stat`` with ``[Errno 63] File name
        # too long``. Treat any OSError as "not a file" and skip.
        try:
            is_file = resolved.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        if resolved.suffix in _SKIP_EXTENSIONS:
            continue
        yield resolved


__all__ = ["run_secretscan"]
