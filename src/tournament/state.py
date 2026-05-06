"""Atomic artifact persistence for tournament runs.

Layout::

    <artifact_dir>/
      initial_a.md
      incumbent_after_NN.md     (one per non-A winning pass)
      final_output.md           (sole "tournament complete" marker)
      history.json
      pass_NN/
        version_a.md            (written after CRITIC starts)
        critic.md               (written after CRITIC completes)
        version_b.md            (written after ARCHITECT_B completes)
        version_ab.md           (written after SYNTHESIZER completes)
        synth_meta.json         (X/Y assignment recorded when SYNTHESIZER completes)
        judges/
          <i>_order.json        (shuffle order, written before judge i runs)
          <i>_response.json     (raw + parsed ranking, written after judge i lands)
        result.json             (sole "pass complete" marker; written after Borda)

Each individual file is written atomically via a same-directory tempfile +
``os.replace``. ``result.json`` is the sole pass-completion marker — its
absence indicates a partial pass that ``read_resume_state`` may surface as
``PartialPassState`` for resume.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tournament.core import PartialPassState, PassResult, ResumeState


_PASS_DIR_RE = re.compile(r"^pass_(\d+)$")
_JUDGE_ORDER_RE = re.compile(r"^(\d+)_order\.json$")
_JUDGE_RESPONSE_RE = re.compile(r"^(\d+)_response\.json$")


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

    def judges_dir(self, pass_num: int) -> Path:
        d = self.pass_dir(pass_num) / "judges"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── per-role writers (Tier 3) ──
    def write_version_a(self, pass_num: int, version_a_md: str) -> Path:
        """Write pass_NN/version_a.md."""
        path = self.pass_dir(pass_num) / "version_a.md"
        _atomic_write_text(path, version_a_md)
        return path

    def write_critic(self, pass_num: int, critic_md: str) -> Path:
        """Write pass_NN/critic.md."""
        path = self.pass_dir(pass_num) / "critic.md"
        _atomic_write_text(path, critic_md)
        return path

    def write_version_b(self, pass_num: int, version_b_md: str) -> Path:
        """Write pass_NN/version_b.md."""
        path = self.pass_dir(pass_num) / "version_b.md"
        _atomic_write_text(path, version_b_md)
        return path

    def write_synthesis(
        self, pass_num: int, version_ab_md: str, synth_meta: dict[str, str]
    ) -> tuple[Path, Path]:
        """Write pass_NN/version_ab.md and pass_NN/synth_meta.json (atomic per file).

        ``synth_meta`` must contain ``x_label`` and ``y_label`` (each ``"A"`` or
        ``"B"``).
        """
        pdir = self.pass_dir(pass_num)
        ab_path = pdir / "version_ab.md"
        meta_path = pdir / "synth_meta.json"
        _atomic_write_text(ab_path, version_ab_md)
        _atomic_write_json(meta_path, synth_meta)
        return ab_path, meta_path

    def write_judge_order(
        self, pass_num: int, judge_index: int, order: dict[int, str]
    ) -> Path:
        """Write pass_NN/judges/<i>_order.json before judge i runs.

        JSON requires string keys, so the int slot keys (1/2/3) are stringified
        on write and converted back on read.
        """
        path = self.judges_dir(pass_num) / f"{judge_index}_order.json"
        # Stringify keys for JSON compatibility.
        serialisable = {str(k): v for k, v in order.items()}
        _atomic_write_json(path, serialisable)
        return path

    def write_judge_response(
        self, pass_num: int, judge_index: int, response: dict[str, Any]
    ) -> Path:
        """Write pass_NN/judges/<i>_response.json after judge i lands.

        ``response`` shape: ``{"raw": str, "ranking": list[str] | None,
        "error": str | None}``.
        """
        path = self.judges_dir(pass_num) / f"{judge_index}_response.json"
        _atomic_write_json(path, response)
        return path

    def write_pass_result(self, pass_num: int, result: "PassResult") -> Path:
        """Write pass_NN/result.json — the sole pass-complete marker."""
        path = self.pass_dir(pass_num) / "result.json"
        _atomic_write_json(path, result.model_dump(mode="json"))
        return path

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
            8. Tier 3: probe ``pass_<N+1>/`` for partial state; if any per-role
               artifact is present (and ``result.json`` is absent), attach a
               ``PartialPassState`` to ``ResumeState.partial``.
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
                continue  # partial dir → ignore for completed-pass tally
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # corrupt → ignore
            if not isinstance(data, dict) or "winner" not in data:
                continue
            pass_results[pass_num] = data

        if not pass_results:
            # No completed passes. But maybe pass_01 has partial state from a
            # crash mid-pass-1? Probe pass_01 for partial state too.
            partial = _read_partial_pass_state(
                self.artifact_dir / "pass_01"
            )
            if partial is None:
                return None
            initial_text = self.read_initial()
            if initial_text is None:
                return None
            return ResumeState(
                starting_pass_num=1,
                incumbent_md=initial_text,
                streak=0,
                completed=False,
                final_md=None,
                partial=partial,
            )

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

        starting_pass_num = n + 1
        partial = _read_partial_pass_state(
            self.artifact_dir / f"pass_{starting_pass_num:02d}"
        )

        return ResumeState(
            starting_pass_num=starting_pass_num,
            incumbent_md=incumbent_md,
            streak=streak,
            completed=False,
            final_md=None,
            partial=partial,
        )


def _read_partial_pass_state(pass_dir: Path) -> "PartialPassState | None":
    """Inspect ``pass_dir`` for partial per-role artifacts.

    Returns:
        ``None`` if:
            - the dir doesn't exist,
            - ``result.json`` exists (pass is complete, not partial), or
            - no per-role artifacts are present.
        Otherwise, a populated ``PartialPassState``.
    """
    from tournament.core import PartialPassState

    if not pass_dir.exists() or not pass_dir.is_dir():
        return None

    # Pass complete → not partial.
    if (pass_dir / "result.json").exists():
        return None

    def _read_text(name: str) -> str | None:
        p = pass_dir / name
        if not p.exists():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def _read_json(name: str) -> dict | None:
        p = pass_dir / name
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    version_a_md = _read_text("version_a.md")
    critic_md = _read_text("critic.md")
    version_b_md = _read_text("version_b.md")
    version_ab_md = _read_text("version_ab.md")
    synth_meta_raw = _read_json("synth_meta.json")
    synth_meta: dict[str, str] | None
    if synth_meta_raw is None:
        synth_meta = None
    else:
        # Ensure values are strings (PartialPassState expects dict[str, str]).
        synth_meta = {str(k): str(v) for k, v in synth_meta_raw.items()}

    judge_orders: dict[int, dict[int, str]] = {}
    judge_responses: dict[int, dict[str, Any]] = {}
    judges_subdir = pass_dir / "judges"
    if judges_subdir.exists() and judges_subdir.is_dir():
        for child in judges_subdir.iterdir():
            if not child.is_file():
                continue
            m_order = _JUDGE_ORDER_RE.match(child.name)
            m_response = _JUDGE_RESPONSE_RE.match(child.name)
            if m_order is not None:
                idx = int(m_order.group(1))
                try:
                    raw = json.loads(child.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(raw, dict):
                    continue
                # Convert string keys back to int (slot indices).
                try:
                    parsed = {int(k): v for k, v in raw.items()}
                except (TypeError, ValueError):
                    continue
                judge_orders[idx] = parsed
            elif m_response is not None:
                idx = int(m_response.group(1))
                try:
                    raw = json.loads(child.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(raw, dict):
                    continue
                judge_responses[idx] = raw

    # If absolutely nothing on disk → no partial state.
    if (
        version_a_md is None
        and critic_md is None
        and version_b_md is None
        and version_ab_md is None
        and synth_meta is None
        and not judge_orders
        and not judge_responses
    ):
        return None

    pass_num = int(pass_dir.name.removeprefix("pass_"))
    return PartialPassState(
        pass_num=pass_num,
        version_a_md=version_a_md,
        critic_md=critic_md,
        version_b_md=version_b_md,
        version_ab_md=version_ab_md,
        synth_meta=synth_meta,
        judge_orders=judge_orders,
        judge_responses=judge_responses,
    )


__all__ = ["TournamentArtifactStore"]
