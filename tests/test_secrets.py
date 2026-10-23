"""Secrets beside the config: what is exported wins, and an empty export is not one."""

from __future__ import annotations

import os

from ledger.config import Config


def cfg(tmp_path, env):
    (tmp_path / "ledger.yml").write_text("org: acme\nproject_keys: [ABC]\n")
    (tmp_path / ".env").write_text(env)
    return str(tmp_path / "ledger.yml")


def test_an_exported_value_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_API_TOKEN", "from-the-shell")
    Config.load(cfg(tmp_path, "LEDGER_API_TOKEN=from-the-file\n"))
    assert os.environ["LEDGER_API_TOKEN"] == "from-the-shell"


def test_an_empty_export_does_not_shadow_the_file(tmp_path, monkeypatch):
    """A compose file passes `${LEDGER_API_TOKEN:-}` for every secret it knows
    of. Left empty there, the value beside the config must still be used -- or
    a server configured exactly as documented refuses to start."""
    monkeypatch.setenv("LEDGER_API_TOKEN", "")
    Config.load(cfg(tmp_path, "LEDGER_API_TOKEN=from-the-file\n"))
    assert os.environ["LEDGER_API_TOKEN"] == "from-the-file"
