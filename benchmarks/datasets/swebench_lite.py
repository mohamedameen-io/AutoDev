"""SWE-bench-Lite instance loader with a lazy, network-optional HuggingFace path.

The loader returns plain ``dict`` instances (``instance_id``, ``repo``,
``base_commit``, ``problem_statement``, ``test_patch``, ``version``,
``environment_setup_commit``, ...) consumed by the host-arm64 solve adapter
(``benchmarks.adapters.swebench_lite``).

Two sources, in priority order:

  1. a **local JSONL** of instances (``--instances-file``, or ``--instances``
     pointing at an existing file) — the fully-offline path used by the pilot and
     the hermetic tests;
  2. the **HuggingFace** dataset (``princeton-nlp/SWE-bench_Lite``) via the
     optional ``datasets`` package — imported **lazily inside a function** so this
     module imports (and the JSONL path works) even when ``datasets`` /
     ``huggingface_hub`` are absent. When the HF path is taken without
     ``datasets`` installed it raises a clear :class:`RuntimeError`, never a bare
     ``ImportError`` at module load.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

# Canonical HuggingFace dataset id for SWE-bench-Lite.
DEFAULT_HF_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_SPLIT = "test"

# Friendly ``--dataset`` aliases → HF dataset ids.
_DATASET_ALIASES = {
    "swe-bench-lite": DEFAULT_HF_DATASET,
    "swe-bench_lite": DEFAULT_HF_DATASET,
    "swebench-lite": DEFAULT_HF_DATASET,
    "lite": DEFAULT_HF_DATASET,
}


def _resolve_dataset_name(dataset: str | None) -> str:
    """Map a ``--dataset`` selector to a concrete HF dataset id.

    Known short aliases resolve to :data:`DEFAULT_HF_DATASET`; an unrecognised
    non-empty value is passed through verbatim (so an operator can point at a
    fork/mirror); ``None`` falls back to the canonical Lite dataset.
    """
    if not dataset:
        return DEFAULT_HF_DATASET
    return _DATASET_ALIASES.get(dataset.strip().lower(), dataset)


def _parse_ids(selector: str | None) -> list[str]:
    """Parse a comma-separated ``instance_id`` selector into a list.

    ``None``/blank → ``[]`` (meaning "no id filter"). Whitespace and empty
    fragments are dropped.
    """
    if not selector:
        return []
    return [part.strip() for part in selector.split(",") if part.strip()]


def load_instances_from_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Read a JSONL file of instance records (one JSON object per line).

    Blank lines are skipped. No lazy/heavy imports — this is the offline path.
    """
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _import_datasets() -> Any:
    """Lazily import the optional ``datasets`` package.

    Kept inside a function (never at module top level) so importing this module
    and using the JSONL path work with ``datasets`` absent. Raises a clear,
    actionable :class:`RuntimeError` (mentioning ``datasets``) rather than letting
    a bare ``ImportError`` escape.
    """
    try:
        import datasets  # noqa: E402  (lazy by design — optional heavy dep)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "Loading SWE-bench-Lite from HuggingFace requires the optional "
            "'datasets' package (pip install datasets). It is intentionally NOT a "
            "hard dependency of the benchmark — provide a local instances JSONL "
            "via --instances-file to run fully offline."
        ) from exc
    return datasets


def load_instances_from_hf(
    *,
    dataset_name: str = DEFAULT_HF_DATASET,
    split: str = DEFAULT_SPLIT,
    instance_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Load instances from the HuggingFace dataset, optionally filtered by id.

    The ``datasets`` import is lazy (see :func:`_import_datasets`); when it is
    absent this raises a clear :class:`RuntimeError`. Order is preserved when no
    id filter is given; with a filter, only matching ``instance_id``s are kept.
    """
    datasets = _import_datasets()
    dataset = datasets.load_dataset(dataset_name, split=split)
    records = [dict(row) for row in dataset]
    if instance_ids:
        wanted = set(instance_ids)
        records = [r for r in records if r.get("instance_id") in wanted]
    return records


def load_instances(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve instances for a solve run from CLI ``args``.

    Priority:
      * an explicit ``--instances-file`` JSONL (offline);
      * ``--instances`` pointing at an existing file → treated as that JSONL;
      * otherwise ``--instances`` is a comma-separated id selector and the HF
        dataset is loaded lazily (filtered to those ids).

    When a JSONL source is combined with an id selector that is NOT the file
    itself, the JSONL is filtered to the selected ids.
    """
    selector: str | None = getattr(args, "instances", None)
    instances_file: str | None = getattr(args, "instances_file", None)

    jsonl_path: Path | None = None
    if instances_file:
        jsonl_path = Path(instances_file)
    elif selector and Path(selector).is_file():
        jsonl_path = Path(selector)

    if jsonl_path is not None:
        records = load_instances_from_jsonl(jsonl_path)
        # Apply an id filter only when the selector is a distinct id-list (not the
        # JSONL path we just read).
        if selector and str(jsonl_path) != selector:
            wanted = set(_parse_ids(selector))
            if wanted:
                records = [r for r in records if r.get("instance_id") in wanted]
        return records

    dataset_name = _resolve_dataset_name(getattr(args, "dataset", None))
    return load_instances_from_hf(
        dataset_name=dataset_name, instance_ids=_parse_ids(selector)
    )
