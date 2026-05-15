import time

from joiner import join_lines


def test_correctness_small():
    assert join_lines(["a", "b", "c"]) == "abc"


def test_correctness_with_newlines():
    assert join_lines(["foo\n", "bar\n"]) == "foo\nbar\n"


def test_correctness_empty():
    assert join_lines([]) == ""


def test_perf_50k_lines_under_one_second():
    # 50,000 ~30-char lines. Linear join: well under 100 ms.
    # Quadratic concat: many seconds.
    lines = [f"line {i:06d} payload payload\n" for i in range(50_000)]
    start = time.perf_counter()
    out = join_lines(lines)
    elapsed = time.perf_counter() - start
    assert out.startswith("line 000000"), "output mangled"
    assert out.endswith("payload payload\n"), "output mangled"
    assert elapsed < 1.0, f"join_lines too slow on 50k lines: {elapsed:.3f}s"
