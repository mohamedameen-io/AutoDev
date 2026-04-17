"""Read/write pydantic-validated evidence bundles.

Each bundle lives at ``.autodev/evidence/{task_id}-{kind}.json``. Patches go
to ``{task_id}.patch``. All writes are atomic (tmp -> os.replace).

The discriminator field is ``kind`` (see :mod:`state.schemas`) so a
``TypeAdapter(Evidence)`` routes a dict into the right subclass without
caller gymnastics.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from autologging import get_logger
from state.paths import evidence_dir, evidence_path, patch_path
from state.schemas import Evidence


logger = get_logger(__name__)

_ADAPTER: TypeAdapter[Evidence] = TypeAdapter(Evidence)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".evidence.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def write_evidence(cwd: Path, task_id: str, evidence: Evidence) -> Path:
    """Write ``evidence`` to ``evidence/{task_id}-{kind}.json`` atomically.

    Returns the absolute path written.
    """
    kind = getattr(evidence, "kind")
    dst = evidence_path(cwd, task_id, kind)
    payload = _ADAPTER.dump_python(evidence, mode="json")
    raw = json.dumps(payload, indent=2).encode("utf-8")
    _atomic_write(dst, raw)
    logger.info("evidence.write", task_id=task_id, kind=kind, path=str(dst))
    return dst


async def read_evidence(cwd: Path, task_id: str, kind: str) -> Evidence | None:
    """Return the evidence object at ``{task_id}-{kind}.json`` or ``None``."""
    path = evidence_path(cwd, task_id, kind)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return _ADAPTER.validate_python(raw)
    except Exception:
        return None


async def list_evidence(cwd: Path, task_id: str) -> list[Evidence]:
    """Return every evidence bundle for ``task_id`` (any ``kind``)."""
    d = evidence_dir(cwd)
    if not d.exists():
        return []
    prefix = f"{task_id}-"
    out: list[Evidence] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or not p.name.startswith(prefix) or not p.name.endswith(".json"):
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            out.append(_ADAPTER.validate_python(raw))
        except Exception:
            continue
    return out


async def write_patch(cwd: Path, task_id: str, diff: str) -> Path:
    """Write raw unified diff text to ``evidence/{task_id}.patch`` atomically."""
    dst = patch_path(cwd, task_id)
    _atomic_write(dst, diff.encode("utf-8"))
    logger.info("evidence.patch_written", task_id=task_id, path=str(dst))
    return dst


__all__ = [
    "list_evidence",
    "read_evidence",
    "write_evidence",
    "write_patch",
]
