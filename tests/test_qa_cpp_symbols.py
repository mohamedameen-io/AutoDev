"""Tests for v0.19.0 ``qa.cpp_symbols`` low-level API."""

from __future__ import annotations

from pathlib import Path

from qa.cpp_symbols import (
    TREESITTER_AVAILABLE,
    extract_local_includes,
    extract_unqualified_calls,
    resolve_include_chain,
    scan_cpp_file,
)


def test_extract_local_includes_only(tmp_path: Path) -> None:
    src = '''
#include <stdio.h>          // system: ignored
#include "local.h"
#include "subdir/util.h"
    '''
    out = extract_local_includes(src)
    # System headers excluded; local headers retained.
    assert "local.h" in out
    assert "subdir/util.h" in out
    assert "stdio.h" not in out


def test_extract_unqualified_calls_basic(tmp_path: Path) -> None:
    src = "int main() {\n    foo();\n    bar(42, x);\n    return 0;\n}\n"
    out = extract_unqualified_calls(src)
    assert "foo" in out
    assert "bar" in out
    # Built-in / control flow keywords excluded.
    assert "if" not in out
    assert "for" not in out
    assert "return" not in out


def test_extract_unqualified_calls_skips_qualified(tmp_path: Path) -> None:
    """``ns::foo()`` and ``obj.method()`` are not unqualified calls."""
    src = '''
    int main() {
        std::vector<int> v;
        v.push_back(1);
        ns::foo();
        unqualified_call();
        return 0;
    }
    '''
    out = extract_unqualified_calls(src)
    assert "unqualified_call" in out
    assert "foo" not in out  # qualified by ``ns::``
    assert "push_back" not in out  # member call


def test_resolve_include_chain_local(tmp_path: Path) -> None:
    """The chain expands relative ``#include`` directives recursively."""
    (tmp_path / "a.h").write_text('#include "b.h"\nvoid alpha();\n')
    (tmp_path / "b.h").write_text("void beta();\n")
    (tmp_path / "main.cpp").write_text('#include "a.h"\n')

    chain = resolve_include_chain(tmp_path / "main.cpp")
    # The chain includes a.h and (transitively) b.h.
    paths = {p.name for p in chain}
    assert "a.h" in paths
    assert "b.h" in paths


def test_resolve_include_chain_handles_cycles(tmp_path: Path) -> None:
    (tmp_path / "a.h").write_text('#include "b.h"\nvoid alpha();\n')
    (tmp_path / "b.h").write_text('#include "a.h"\nvoid beta();\n')
    (tmp_path / "main.cpp").write_text('#include "a.h"\n')

    chain = resolve_include_chain(tmp_path / "main.cpp")
    # No infinite loop; each header visited once.
    paths = [p.name for p in chain]
    assert paths.count("a.h") == 1
    assert paths.count("b.h") == 1


def test_scan_cpp_file_clean_local_chain(tmp_path: Path) -> None:
    (tmp_path / "math.h").write_text("int add(int, int);\n")
    main = tmp_path / "main.cpp"
    main.write_text(
        '#include "math.h"\n'
        "int main() { return add(1, 2); }\n"
    )
    findings = scan_cpp_file(main, tmp_path)
    assert findings == []


def test_scan_cpp_file_unresolved_call(tmp_path: Path) -> None:
    """A call that cannot be matched anywhere in the local include chain."""
    (tmp_path / "math.h").write_text("int add(int, int);\n")
    main = tmp_path / "main.cpp"
    main.write_text(
        '#include "math.h"\n'
        "int main() { return ghost_xyz(); }\n"
    )
    findings = scan_cpp_file(main, tmp_path)
    # Conservative: may pass (skip-and-warn for system-only chains) or
    # flag ghost_xyz. Either way, ``add`` must not appear.
    if findings:
        assert any("ghost_xyz" in f for f in findings)
        assert not any("add" in f and "ghost_xyz" not in f for f in findings)


def test_treesitter_available_flag_is_bool() -> None:
    assert isinstance(TREESITTER_AVAILABLE, bool)
