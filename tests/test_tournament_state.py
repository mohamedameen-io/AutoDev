"""Direct unit tests for ``TournamentArtifactStore`` per-role writers and the
``_read_partial_pass_state`` module-level helper (Tier 3).

These tests exercise the per-file writer surface introduced in Tier 3
(:mod:`tournament.state`) without going through ``Tournament.run``. They verify:

  * each per-role writer creates the expected file under
    ``<artifact_dir>/pass_NN/...`` with the expected content,
  * ``write_synthesis`` emits both ``version_ab.md`` and ``synth_meta.json``,
  * judge orders/responses land under ``pass_NN/judges/`` with stringified keys
    on disk (JSON requires string keys) and int keys back in memory,
  * ``write_pass_result`` round-trips through ``model_dump(mode="json")``,
  * ``_read_partial_pass_state`` reports ``None`` for complete/empty/missing
    pass dirs and a populated ``PartialPassState`` otherwise — including int-
    key conversion for judge slot keys and partial-judges subset handling.
"""

from __future__ import annotations

import json
from pathlib import Path

from tournament.core import PartialPassState, PassResult
from tournament.state import TournamentArtifactStore, _read_partial_pass_state


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_pass_result(pass_num: int = 1) -> PassResult:
    """Build a minimal valid ``PassResult`` (mirrors ``core.run_pass`` shape)."""
    return PassResult(
        pass_num=pass_num,
        winner="A",
        scores={"A": 6, "B": 3, "AB": 0},
        valid_judges=3,
        elapsed_s=0.123,
        judge_details=[
            {
                "ranking": ["A", "B", "AB"],
                "order": {"1": "A", "2": "B", "3": "AB"},
                "raw_response": "RANKING: 1, 2, 3",
            }
        ],
        incumbent_hash_before="0123456789abcdef",
        incumbent_hash_after="fedcba9876543210",
        meta={"timestamp": 1234.0},
    )


# ── Per-role writer tests ──────────────────────────────────────────────────


def test_write_version_a_atomic(tmp_path: Path) -> None:
    """``write_version_a`` writes ``pass_NN/version_a.md`` atomically."""
    store = TournamentArtifactStore(tmp_path)
    path = store.write_version_a(1, "hello")
    assert path == tmp_path / "pass_01" / "version_a.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "hello"


def test_write_critic_atomic(tmp_path: Path) -> None:
    """``write_critic`` writes ``pass_NN/critic.md`` atomically."""
    store = TournamentArtifactStore(tmp_path)
    path = store.write_critic(1, "critique here")
    assert path == tmp_path / "pass_01" / "critic.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "critique here"


def test_write_version_b_atomic(tmp_path: Path) -> None:
    """``write_version_b`` writes ``pass_NN/version_b.md`` atomically."""
    store = TournamentArtifactStore(tmp_path)
    path = store.write_version_b(1, "B revision")
    assert path == tmp_path / "pass_01" / "version_b.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "B revision"


def test_write_synthesis_writes_two_files(tmp_path: Path) -> None:
    """``write_synthesis`` emits both ``version_ab.md`` and ``synth_meta.json``."""
    store = TournamentArtifactStore(tmp_path)
    ab_path, meta_path = store.write_synthesis(
        1, "AB content", {"x_label": "A", "y_label": "B"}
    )

    assert ab_path == tmp_path / "pass_01" / "version_ab.md"
    assert meta_path == tmp_path / "pass_01" / "synth_meta.json"
    assert ab_path.exists()
    assert meta_path.exists()

    assert ab_path.read_text(encoding="utf-8") == "AB content"
    parsed_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert parsed_meta == {"x_label": "A", "y_label": "B"}


def test_write_judge_order_creates_judges_subdir(tmp_path: Path) -> None:
    """``write_judge_order`` creates ``judges/`` and stringifies int keys."""
    store = TournamentArtifactStore(tmp_path)
    order: dict[int, str] = {1: "B", 2: "AB", 3: "A"}
    path = store.write_judge_order(1, 0, order)

    judges_dir = tmp_path / "pass_01" / "judges"
    assert judges_dir.exists() and judges_dir.is_dir()
    assert path == judges_dir / "0_order.json"
    assert path.exists()

    # JSON requires string keys — verify on-disk shape uses "1"/"2"/"3".
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {"1": "B", "2": "AB", "3": "A"}
    assert all(isinstance(k, str) for k in raw)


def test_write_judge_response_atomic(tmp_path: Path) -> None:
    """``write_judge_response`` round-trips a response dict through JSON."""
    store = TournamentArtifactStore(tmp_path)
    response = {
        "raw": "RANKING: 1, 2, 3",
        "ranking": ["B", "AB", "A"],
        "error": None,
    }
    path = store.write_judge_response(1, 0, response)

    assert path == tmp_path / "pass_01" / "judges" / "0_response.json"
    assert path.exists()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed == response


def test_write_pass_result_atomic(tmp_path: Path) -> None:
    """``write_pass_result`` round-trips through ``model_dump(mode="json")``."""
    store = TournamentArtifactStore(tmp_path)
    result = _make_pass_result(pass_num=1)
    path = store.write_pass_result(1, result)

    assert path == tmp_path / "pass_01" / "result.json"
    assert path.exists()

    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed == result.model_dump(mode="json")


# ── _read_partial_pass_state tests ──────────────────────────────────────────


def test_read_partial_pass_state_returns_none_when_pass_complete(
    tmp_path: Path,
) -> None:
    """A pass dir with ``result.json`` is complete → returns ``None``."""
    pass_dir = tmp_path / "pass_01"
    pass_dir.mkdir()
    result = _make_pass_result(pass_num=1)
    (pass_dir / "result.json").write_text(
        json.dumps(result.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    # Even if other partial artifacts exist, the presence of result.json wins.
    (pass_dir / "critic.md").write_text("partial critic", encoding="utf-8")

    assert _read_partial_pass_state(pass_dir) is None


def test_read_partial_pass_state_returns_state_when_partial(tmp_path: Path) -> None:
    """A pass dir with only ``critic.md`` returns a populated state."""
    pass_dir = tmp_path / "pass_03"
    pass_dir.mkdir()
    (pass_dir / "critic.md").write_text("only a critic", encoding="utf-8")

    state = _read_partial_pass_state(pass_dir)
    assert state is not None
    assert isinstance(state, PartialPassState)
    assert state.pass_num == 3
    assert state.critic_md == "only a critic"
    # All other text fields absent.
    assert state.version_a_md is None
    assert state.version_b_md is None
    assert state.version_ab_md is None
    assert state.synth_meta is None
    assert state.judge_orders == {}
    assert state.judge_responses == {}


def test_read_partial_pass_state_returns_none_when_empty(tmp_path: Path) -> None:
    """An empty pass dir returns ``None`` (nothing to resume)."""
    pass_dir = tmp_path / "pass_01"
    pass_dir.mkdir()
    assert _read_partial_pass_state(pass_dir) is None


def test_read_partial_pass_state_returns_none_when_dir_missing(
    tmp_path: Path,
) -> None:
    """A non-existent pass dir returns ``None``."""
    missing = tmp_path / "pass_99"
    assert not missing.exists()
    assert _read_partial_pass_state(missing) is None


def test_read_partial_pass_state_with_judges_subset(tmp_path: Path) -> None:
    """Subset of judges on disk returns int-keyed dicts for present judges."""
    pass_dir = tmp_path / "pass_03"
    judges_dir = pass_dir / "judges"
    judges_dir.mkdir(parents=True)

    # Judge 0: response only (no order). Judge 2: order only (no response).
    # Judge 1: skipped entirely.
    response_0 = {
        "raw": "RANKING: 1, 2, 3",
        "ranking": ["1", "2", "3"],
        "error": None,
    }
    (judges_dir / "0_response.json").write_text(
        json.dumps(response_0, indent=2), encoding="utf-8"
    )

    order_2 = {1: "AB", 2: "A", 3: "B"}
    (judges_dir / "2_order.json").write_text(
        json.dumps({str(k): v for k, v in order_2.items()}, indent=2),
        encoding="utf-8",
    )

    state = _read_partial_pass_state(pass_dir)
    assert state is not None
    assert state.pass_num == 3

    # Outer keys are judge indices (int). Inner order keys are slot ints (1/2/3).
    assert set(state.judge_responses.keys()) == {0}
    assert all(isinstance(k, int) for k in state.judge_responses)
    assert state.judge_responses[0] == response_0

    assert set(state.judge_orders.keys()) == {2}
    assert all(isinstance(k, int) for k in state.judge_orders)
    assert state.judge_orders[2] == order_2  # int slot keys restored
    assert all(isinstance(k, int) for k in state.judge_orders[2])


def test_read_partial_pass_state_full_pass_minus_result(tmp_path: Path) -> None:
    """All per-role files present (no ``result.json``) → fully populated state."""
    pass_dir = tmp_path / "pass_05"
    judges_dir = pass_dir / "judges"
    judges_dir.mkdir(parents=True)

    (pass_dir / "version_a.md").write_text("A_TEXT", encoding="utf-8")
    (pass_dir / "critic.md").write_text("CRITIC_TEXT", encoding="utf-8")
    (pass_dir / "version_b.md").write_text("B_TEXT", encoding="utf-8")
    (pass_dir / "version_ab.md").write_text("AB_TEXT", encoding="utf-8")

    synth_meta = {"x_label": "A", "y_label": "B"}
    (pass_dir / "synth_meta.json").write_text(
        json.dumps(synth_meta, indent=2), encoding="utf-8"
    )

    orders: dict[int, dict[int, str]] = {
        0: {1: "A", 2: "B", 3: "AB"},
        1: {1: "B", 2: "AB", 3: "A"},
        2: {1: "AB", 2: "A", 3: "B"},
    }
    responses: dict[int, dict[str, object]] = {
        0: {"raw": "RANKING: 1, 2, 3", "ranking": ["1", "2", "3"], "error": None},
        1: {"raw": "RANKING: 2, 3, 1", "ranking": ["2", "3", "1"], "error": None},
        2: {"raw": "RANKING: 3, 1, 2", "ranking": ["3", "1", "2"], "error": None},
    }
    for idx, order in orders.items():
        (judges_dir / f"{idx}_order.json").write_text(
            json.dumps({str(k): v for k, v in order.items()}, indent=2),
            encoding="utf-8",
        )
    for idx, resp in responses.items():
        (judges_dir / f"{idx}_response.json").write_text(
            json.dumps(resp, indent=2), encoding="utf-8"
        )

    state = _read_partial_pass_state(pass_dir)
    assert state is not None
    assert state.pass_num == 5
    assert state.version_a_md == "A_TEXT"
    assert state.critic_md == "CRITIC_TEXT"
    assert state.version_b_md == "B_TEXT"
    assert state.version_ab_md == "AB_TEXT"
    assert state.synth_meta == synth_meta

    assert len(state.judge_orders) == 3
    assert len(state.judge_responses) == 3
    assert set(state.judge_orders.keys()) == {0, 1, 2}
    assert set(state.judge_responses.keys()) == {0, 1, 2}
    for idx, order in orders.items():
        assert state.judge_orders[idx] == order  # int keys restored
    for idx, resp in responses.items():
        assert state.judge_responses[idx] == resp
