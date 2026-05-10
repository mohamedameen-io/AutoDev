"""v0.24.0 D5 regression: ``RepoCapacity`` shape signals.

The original probe collected ``file_count``, ``total_bytes``,
``depth_max``, and ``is_huge``. v0.24.0 D5 extends with shape signals
that downstream tuners (sparse-checkout pattern derivation, future QA
gate sandboxing) consume:

* ``avg_file_size_bytes``
* ``largest_dir`` + ``largest_dir_file_count``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.repo_probe import RepoCapacity, _largest_directory, probe_repo


def _init_git(p: Path, files: dict[str, str]) -> None:
    """Initialize a git repo at *p* with the given relative files."""
    subprocess.run(["git", "init", str(p)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(p), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(p), check=True)
    for rel, content in files.items():
        f = p / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        subprocess.run(["git", "add", rel], cwd=str(p), check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(p),
        check=True,
        capture_output=True,
    )


def test_repo_capacity_has_shape_fields() -> None:
    """Direct constructor accepts the new fields with defaults."""
    rc = RepoCapacity(file_count=10, total_bytes=1000, depth_max=2, is_huge=False)
    assert rc.avg_file_size_bytes == 0
    assert rc.largest_dir == ""
    assert rc.largest_dir_file_count == 0


def test_avg_file_size_handles_empty_repo(tmp_path: Path) -> None:
    """Empty repo: avg_file_size_bytes is 0 (no division by zero)."""
    capacity = probe_repo(tmp_path)
    assert capacity.avg_file_size_bytes == 0


def test_avg_file_size_basic(tmp_path: Path) -> None:
    """5 files × 100 bytes => avg = 100."""
    _init_git(
        tmp_path,
        {
            f"src/f{i}.py": "x" * 100 for i in range(5)
        },
    )
    capacity = probe_repo(tmp_path)
    assert capacity.file_count == 5
    # avg = total // count; allow ±10% wiggle for git overhead in total_bytes.
    assert 80 <= capacity.avg_file_size_bytes <= 120


def test_largest_directory_finds_busiest(tmp_path: Path) -> None:
    """The directory with the most files wins."""
    _init_git(
        tmp_path,
        {
            "small/a.py": "x",
            "src/foo.py": "x",
            "src/bar.py": "x",
            "src/baz.py": "x",
            "src/qux.py": "x",
        },
    )
    capacity = probe_repo(tmp_path)
    assert capacity.largest_dir == "src"
    assert capacity.largest_dir_file_count == 4


def test_largest_directory_helper_empty_repo(tmp_path: Path) -> None:
    """Outside a git repo with no files: ``("", 0)`` (graceful)."""
    rel, n = _largest_directory(tmp_path)
    assert rel == ""
    assert n == 0


def test_repo_capacity_logs_shape_fields(tmp_path: Path) -> None:
    """Probe completes without raising and populates the new fields."""
    _init_git(
        tmp_path,
        {"src/main.py": "print('hi')\n", "tests/test_main.py": "import main\n"},
    )
    capacity = probe_repo(tmp_path)
    assert capacity.file_count == 2
    assert capacity.avg_file_size_bytes > 0
    assert capacity.largest_dir in ("src", "tests")
    assert capacity.largest_dir_file_count == 1
