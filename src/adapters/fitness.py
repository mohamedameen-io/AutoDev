"""Adapter fitness scoring against a codebase language profile.

v0.31.0 (Phase 5.4): each adapter is scored 0-100 against the codebase's
language profile so the CLI can warn when the operator picks an adapter
that's a poor match for what they're working on. Scoring is deliberately
conservative -- a low score is a *warning*, never a hard block (operators
who know what they're doing should be able to opt in regardless).

Initial heuristics (refined over time as we learn from fleet data):

* ``cursor`` -- biased toward TypeScript / JavaScript codebases because
  Cursor's training data and tooling are strongest there. Scores 95 if
  TS+JS share is >= 50%, 80 if >= 30%, 60 if >= 10%, 30 otherwise.
* ``claude_code`` -- broadly capable across languages; baseline 85, +5
  for Python-heavy projects (>= 40% python share).
* Unknown adapter -- 50 (no opinion; surfaces neither warning nor
  enthusiasm).

Consumers:

* :func:`cli.commands.execute.execute` and :func:`plan.plan` print a
  user-facing warning when ``score < 50``.
* :func:`adapters.detect.detect_platform` (Phase 5.5) optionally factors
  the score into the auto-selection decision when
  ``AUTODEV_LANG_WEIGHT > 0``.
* :func:`cli.commands.doctor.doctor` (Phase 5.6) surfaces the score
  alongside the language profile.
"""

from __future__ import annotations

from typing import Final


WARNING_THRESHOLD: Final[float] = 50.0
_UNKNOWN_BASELINE: Final[float] = 50.0


def _ts_js_share(profile: dict[str, float]) -> float:
    return float(profile.get("typescript", 0.0)) + float(
        profile.get("javascript", 0.0)
    )


def compute_fitness_score(
    adapter_name: str, profile: dict[str, float]
) -> float:
    """Score 0-100 (higher = better fit) for ``adapter_name`` against ``profile``.

    See module docstring for the per-adapter rules. Returns
    :data:`_UNKNOWN_BASELINE` (50) for unknown adapter names so callers
    surface neither warning nor enthusiasm.
    """
    name = (adapter_name or "").strip().lower()

    if name == "cursor":
        ts_js = _ts_js_share(profile)
        if ts_js >= 0.50:
            return 95.0
        if ts_js >= 0.30:
            return 80.0
        if ts_js >= 0.10:
            return 60.0
        return 30.0

    if name == "claude_code":
        score = 85.0
        if float(profile.get("python", 0.0)) >= 0.40:
            score += 5.0
        return score

    return _UNKNOWN_BASELINE


def get_fitness_warning(
    adapter_name: str, profile: dict[str, float]
) -> str | None:
    """Return a user-facing warning when ``score < 50``, else ``None``.

    The warning summarises the codebase profile so the operator can
    decide whether to keep the adapter or switch.
    """
    score = compute_fitness_score(adapter_name, profile)
    if score >= WARNING_THRESHOLD:
        return None
    top = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)[:3]
    top_str = ", ".join(f"{lang} {share:.0%}" for lang, share in top)
    return (
        f"Adapter '{adapter_name}' scores {score:.0f}/100 against this "
        f"codebase (top languages: {top_str}). Consider switching adapters "
        "or set AUTODEV_PLATFORM to override."
    )


__all__ = [
    "WARNING_THRESHOLD",
    "compute_fitness_score",
    "get_fitness_warning",
]
