#!/usr/bin/env python3
"""Synthesise ~50k filler Python modules around the real needle (core/calc.py).

The filler is intentionally NOT committed to the AutoDev repo (it is a few
hundred MB of block-allocated tiny files; see the repo-root .gitignore entry
for ``benchmarks/tasks/v1/task_009_py_50k_scale/repo/filler/``). Regenerate it
before a Phase-4 P6 run:

    python benchmarks/tasks/v1/task_009_py_50k_scale/generate_filler.py

It is idempotent (clears ``filler/`` first). Delete ``repo/filler/`` after the
run to reclaim disk.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

PKGS = 250
MODS_PER_PKG = 200  # 250 * 200 = 50,000 modules


def main() -> int:
    repo = Path(__file__).resolve().parent / "repo"
    filler = repo / "filler"
    if filler.exists():
        shutil.rmtree(filler)
    filler.mkdir(parents=True, exist_ok=True)
    (filler / "__init__.py").write_text("", encoding="utf-8")
    t0 = time.time()
    n = 0
    for p in range(PKGS):
        pkg = filler / f"pkg{p:03d}"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        for m in range(MODS_PER_PKG):
            (pkg / f"mod_{m:03d}.py").write_text(
                f'"""Filler module {p:03d}/{m:03d}."""\n\n'
                f"def value_{p:03d}_{m:03d}(x: int) -> int:\n"
                f"    return x + {p * MODS_PER_PKG + m}\n",
                encoding="utf-8",
            )
            n += 1
    print(f"generated {n} filler modules under {filler} in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
