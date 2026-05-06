"""Atomic artifact persistence for tournament runs.

Layout::

    <artifact_dir>/
      initial_a.md
      incumbent_after_NN.md     (one per non-A winning pass)
      final_output.md
      history.json
      pass_NN/
        version_a.md
        critic.md
        version_b.md
        version_ab.md
        result.json

All writes are atomic (tmp file in the same directory, then `os.replace`).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tournament.core import PassResult, ResumeState


_PASS_DIR_RE = re.compile(r"^pass_(\d+)$")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via a same-directory tempfile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Temp file must be in the same directory so os.replace is atomic (same FS).
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, obj: object) -> None:
    payload = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, payload)


class TournamentArtifactStore:
    """Writes tournament artifacts to disk under a single `artifact_dir`."""

    def __init__(self, artifact_dir: Path) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    # ── initial / incumbent / final ──
    def write_initial(self, a_md: str) -> Path:
        path = self.artifact_dir / "initial_a.md"
        _atomic_write_text(path, a_md)
        return path

    def write_incumbent_after(self, pass_num: int, a_md: str) -> Path:
        path = self.artifact_dir / f"incumbent_after_{pass_num:02d}.md"
        _atomic_write_text(path, a_md)
        return path

    def write_final(self, final_md: str, history: list["PassResult"]) -> tuple[Path, Path]:
        final_path = self.artifact_dir / "final_output.md"
        _atomic_write_text(final_path, final_md)

        history_path = self.artifact_dir / "history.json"
        serialised = [h.model_dump(mode="json") for h in history]
        _atomic_write_json(history_path, serialised)
        return final_path, history_path

    # ── per-pass artifacts ──
    def pass_dir(self, pass_num: int) -> Path:
        d = self.artifact_dir / f"pass_{pass_num:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_pass(
        self,
        pass_num: int,
        version_a_md: str,
        critic_md: str,
        version_b_md: str,
        version_ab_md: str,
        result: "PassResult",
    ) -> Path:
        """Write all artifacts for a single pass atomically."""
        pdir = self.pass_dir(pass_num)
        _atomic_write_text(pdir / "version_a.md", version_a_md)
        _atomic_write_text(pdir / "critic.md", critic_md)
        _atomic_write_text(pdir / "version_b.md", version_b_md)
        _atomic_write_text(pdir / "version_ab.md", version_ab_md)
        _atomic_write_json(pdir / "result.json", result.model_dump(mode="json"))
        return pdir

    # ── readers (Tier 2D resume support) ──
    def read_initial(self) -> str | None:
        """Return contents of ``initial_a.md`` or ``None`` if absent."""
        path = self.artifact_dir / "initial_a.md"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def read_history(self) -> list[dict] | None:
        """Return ``history.json`` parsed as a list[dict] or ``None`` if absent."""
        path = self.artifact_dir / "history.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        return data

    def read_resume_state(self) -> "ResumeState | None":
        """Infer resume state from on-disk artifacts.

        Returns:
            ``ResumeState`` if any progress is detected (a complete pass result
            or a final output). ``None`` indicates no resumable progress
            (caller should start fresh).

        Algorithm:
            1. If ``final_output.md`` exists → ``completed=True`` short-circuit.
            2. List ``pass_*/result.json`` files; sort by integer pass number.
            3. Skip pass dirs without parseable ``result.json`` (partial crash).
            4. If no complete passes → return ``None`` (fresh start).
            5. Highest pass ``N`` determines ``starting_pass_num = N+1``.
            6. Resolve incumbent from winner of pass ``N``:
                - winner=A: walk back to the highest ``M ≤ N`` with non-A
                  winner. If found, incumbent = ``incumbent_after_M.md``.
                  Otherwise fall back to ``initial_a.md``.
                - winner=B/AB: prefer ``incumbent_after_N.md``; fall back to
                  ``pass_N/version_b.md`` (B) or ``pass_N/version_ab.md`` (AB)
                  if the incumbent_after file is missing (rare crash window).
            7. ``streak`` = trailing A-wins at pass ``N`` (counting back).
        """
        # Import locally to avoid module-load circular import; core imports
        # state under TYPE_CHECKING only, but state's runtime needs the dataclass.
        from tournament.core import ResumeState

        final_path = self.artifact_dir / "final_output.md"
        if final_path.exists():
            try:
                final_text = final_path.read_text(encoding="utf-8")
            except OSError:
                final_text = ""
            return ResumeState(
                starting_pass_num=0,
                incumbent_md="",
                streak=0,
                completed=True,
                final_md=final_text,
            )

        if not self.artifact_dir.exists():
            return None

        # Collect pass numbers with parseable result.json.
        pass_results: dict[int, dict] = {}
        for child in self.artifact_dir.iterdir():
            if not child.is_dir():
                continue
            m = _PASS_DIR_RE.match(child.name)
            if not m:
                continue
            pass_num = int(m.group(1))
            result_path = child / "result.json"
            if not result_path.exists():
                continue  # partial dir → ignore
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # corrupt → ignore
            if not isinstance(data, dict) or "winner" not in data:
                continue
            pass_results[pass_num] = data

        if not pass_results:
            return None

        sorted_nums = sorted(pass_results.keys())
        n = sorted_nums[-1]
        last = pass_results[n]
        winner = last.get("winner")

        # Resolve incumbent.
        if winner == "A":
            # Walk back to highest M ≤ N with non-A winner.
            incumbent_md: str | None = None
            for m_num in reversed(sorted_nums):
                if m_num > n:
                    continue
                w = pass_results[m_num].get("winner")
                if w != "A":
                    inc_after = self.artifact_dir / f"incumbent_after_{m_num:02d}.md"
                    if inc_after.exists():
                        incumbent_md = inc_after.read_text(encoding="utf-8")
                    else:
                        # Fallback: read pass version directly.
                        v_file = (
                            self.artifact_dir / f"pass_{m_num:02d}" / "version_b.md"
                            if w == "B"
                            else self.artifact_dir / f"pass_{m_num:02d}" / "version_ab.md"
                        )
                        if v_file.exists():
                            incumbent_md = v_file.read_text(encoding="utf-8")
                    break
            if incumbent_md is None:
                # No prior non-A win — fall back to initial_a.md.
                initial_text = self.read_initial()
                if initial_text is None:
                    # No initial on disk either → cannot resume.
                    return None
                incumbent_md = initial_text
        elif winner in ("B", "AB"):
            inc_after = self.artifact_dir / f"incumbent_after_{n:02d}.md"
            if inc_after.exists():
                incumbent_md = inc_after.read_text(encoding="utf-8")
            else:
                v_name = "version_b.md" if winner == "B" else "version_ab.md"
                v_file = self.artifact_dir / f"pass_{n:02d}" / v_name
                if v_file.exists():
                    incumbent_md = v_file.read_text(encoding="utf-8")
                else:
                    return None
        else:
            return None  # unknown winner label

        # Compute trailing A-streak ending at pass n.
        streak = 0
        for m_num in reversed(sorted_nums):
            if m_num > n:
                continue
            if pass_results[m_num].get("winner") == "A":
                streak += 1
            else:
                break

        return ResumeState(
            starting_pass_num=n + 1,
            incumbent_md=incumbent_md,
            streak=streak,
            completed=False,
            final_md=None,
        )


__all__ = ["TournamentArtifactStore"]
