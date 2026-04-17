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
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tournament.core import PassResult


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


__all__ = ["TournamentArtifactStore"]
