"""Validate and bump AutoDev's release version.

Usage
-----
    python scripts/release_version.py X.Y.Z [--allow-npm-mismatch] [--check-only]

Validates::

    1. Version is semver `X.Y.Z` (no prefix, no pre-release suffix).
    2. ``CHANGELOG.md`` has a ``## [X.Y.Z]`` heading.
    3. ``npm/package.json`` version matches X.Y.Z, OR ``--allow-npm-mismatch``
       is set (today the npm package is intentionally decoupled at 0.24.0).

If validation passes, ``src/_version.py`` is updated and next-step
instructions are printed. With ``--check-only`` no files are written —
useful for CI preflight gates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_PATH = REPO_ROOT / "src" / "_version.py"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
NPM_PACKAGE_PATH = REPO_ROOT / "npm" / "package.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ValidationError(RuntimeError):
    """Raised when the requested version is invalid or inconsistent."""


def validate_format(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise ValidationError(
            f"version {version!r} is not semver X.Y.Z "
            "(no leading 'v', no pre-release suffix)"
        )


def read_current_version(path: Path = VERSION_PATH) -> str:
    text = path.read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise ValidationError(f"could not parse __version__ from {path}")
    return m.group(1)


def validate_changelog(version: str, path: Path = CHANGELOG_PATH) -> None:
    if not path.exists():
        raise ValidationError(f"CHANGELOG.md not found at {path}")
    text = path.read_text()
    needle = f"## [{version}]"
    if needle not in text:
        raise ValidationError(
            f"CHANGELOG.md is missing a '{needle}' heading. "
            "Add the entry before bumping."
        )


def validate_npm(
    version: str,
    *,
    allow_mismatch: bool = False,
    path: Path = NPM_PACKAGE_PATH,
) -> str | None:
    """Return a warning string if the npm package version diverges, else None."""
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    npm_version = raw.get("version", "")
    if npm_version == version:
        return None
    msg = (
        f"npm/package.json is at {npm_version!r}, not {version!r}"
    )
    if not allow_mismatch:
        raise ValidationError(
            msg + " — pass --allow-npm-mismatch if this is intentional"
        )
    return msg + " (allowed via --allow-npm-mismatch)"


def write_version(version: str, path: Path = VERSION_PATH) -> None:
    text = path.read_text()
    new_text = re.sub(
        r'(__version__\s*=\s*)"[^"]+"',
        rf'\1"{version}"',
        text,
        count=1,
    )
    if new_text == text and f'"{version}"' not in text:
        raise ValidationError(f"failed to rewrite __version__ in {path}")
    path.write_text(new_text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("version", help="Target version, e.g. 0.31.0")
    p.add_argument(
        "--allow-npm-mismatch",
        action="store_true",
        help="Tolerate npm/package.json being on a different version (decoupled releases).",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Validate only; do not write _version.py.",
    )
    args = p.parse_args(argv)
    version: str = args.version

    try:
        validate_format(version)
        validate_changelog(version)
        npm_warning = validate_npm(
            version, allow_mismatch=args.allow_npm_mismatch
        )
    except ValidationError as exc:
        print(f"release_version: {exc}", file=sys.stderr)
        return 1

    if npm_warning:
        print(f"release_version: warning: {npm_warning}")

    if args.check_only:
        print(f"release_version: OK (check-only) — {version} is releasable.")
        return 0

    try:
        write_version(version)
    except ValidationError as exc:
        print(f"release_version: {exc}", file=sys.stderr)
        return 1

    print(f"release_version: bumped src/_version.py to {version}.")
    print("Next steps:")
    print(f"  git commit -am 'chore(release): bump to {version}'")
    print("  git push origin main")
    print(f"  # then trigger .github/workflows/release.yml with version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
