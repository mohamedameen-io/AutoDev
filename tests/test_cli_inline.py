"""CLI smoke test for the deprecated ``autodev init --inline`` flag.

v0.26.0: InlineAdapter and the inline-mode CLI flows (execute exiting
on :class:`DelegationPendingSignal`, resume gating on
``inline-state.json``) were removed. The ``--inline`` flag survives as
a deprecated noop alias on ``autodev init`` until v0.27.0 — this
single test pins that behavior so the deprecation path doesn't quietly
break.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cli import cli


def test_init_inline_flag_is_deprecated_noop(tmp_path: Path) -> None:
    """``autodev init --inline`` must:

    * exit 0
    * emit a deprecation warning in stdout (so operators upgrading from
      <=v0.25.x notice the change)
    * write ``platform: "claude_code"`` to ``.autodev/config.json``
      (NOT ``"inline"`` — InlineAdapter was deleted)
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)

        result = runner.invoke(cli, ["init", "--inline"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        # The deprecation message must appear on the console so operators
        # are aware why their workspace is now subprocess-mode.
        assert "deprecated" in result.output.lower()
        assert "--inline" in result.output
        # And the persisted platform must be claude_code, not "inline".
        config_file = cwd / ".autodev" / "config.json"
        assert config_file.exists(), "config.json was not created"
        config_data = json.loads(config_file.read_text(encoding="utf-8"))
        assert config_data["platform"] == "claude_code"
