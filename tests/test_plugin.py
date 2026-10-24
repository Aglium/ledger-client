"""The plugin bundle is files, and files drift. These hold it to the package."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
RUNNER = PLUGIN / "scripts" / "ledger-run.sh"


def test_the_plugin_version_is_the_client_version():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    client = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert manifest["version"] == client["project"]["version"]


def test_the_runner_fetches_the_same_release_with_the_mcp_extra():
    """An unpinned fetch runs whatever is newest; one without `mcp` cannot start the server."""
    import re
    text = RUNNER.read_text()
    client = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert re.search(r"^LEDGER_VERSION=(\S+)$", text, re.M).group(1) == client["project"]["version"]
    assert '"aglium-ledger[mcp]==${LEDGER_VERSION}"' in text


def test_only_the_push_time_hook_is_registered():
    """Session start and the prompt hook stay out until they are measured again."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert list(hooks) == ["PreToolUse"] and hooks["PreToolUse"][0]["matcher"] == "Bash"


def test_every_command_the_plugin_names_exists():
    from ledger.cli import app
    have = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert {"init", "brief", "replay", "hook", "mcp", "mirrors", "coverage"} <= have
    assert "feedback" in {g.name for g in app.registered_groups}


def hook(payload: dict, **env) -> subprocess.CompletedProcess:
    # A PATH with neither the client nor uvx on it: nothing to run, nothing to fetch.
    return subprocess.run([str(RUNNER), "push-hook"], input=json.dumps(payload), text=True,
                          capture_output=True, env={"PATH": "/usr/bin:/bin", **env})


class TestTheRunnerNeverGetsInTheWay:
    def test_a_command_that_is_not_a_push_starts_nothing(self, tmp_path):
        marker = tmp_path / "ran"
        fake = tmp_path / "ledger"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
        fake.chmod(0o755)
        r = hook({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, LEDGER_BIN=str(fake))
        assert (r.returncode, r.stdout) == (0, "") and not marker.exists()

    def test_a_push_reaches_ledger_with_the_payload(self, tmp_path):
        seen = tmp_path / "seen"
        fake = tmp_path / "ledger"
        fake.write_text(f'#!/bin/sh\necho "$1" > {seen}\ncat >> {seen}\necho \'{{"ok":1}}\'\n')
        fake.chmod(0o755)
        r = hook({"tool_name": "Bash", "tool_input": {"command": "git push origin feat/x"}},
                 LEDGER_BIN=str(fake))
        assert r.returncode == 0 and json.loads(r.stdout) == {"ok": 1}
        assert seen.read_text().startswith("hook\n") and "git push origin feat/x" in seen.read_text()

    def test_with_no_ledger_installed_the_push_goes_ahead_in_silence(self):
        r = hook({"tool_name": "Bash", "tool_input": {"command": "git push"}})
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")

    def test_a_ledger_that_crashes_does_not_fail_the_push(self, tmp_path):
        fake = tmp_path / "ledger"
        fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
        fake.chmod(0o755)
        r = hook({"tool_name": "Bash", "tool_input": {"command": "gh pr create"}}, LEDGER_BIN=str(fake))
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")

    def test_a_command_with_no_ledger_says_how_to_get_one(self):
        r = subprocess.run([str(RUNNER), "brief", "a", "b"], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin"})
        assert r.returncode == 127 and "not installed" in r.stderr


def _prose_words(path: Path) -> int:
    import re
    return len(re.sub(r"```.*?```", "", path.read_text(), flags=re.S).split())


def test_the_plugin_has_a_readme_and_a_licence():
    """The plugin directory refuses a plugin without them (README: 40+ words outside code)."""
    assert _prose_words(PLUGIN / "README.md") >= 40
    assert (PLUGIN / "LICENSE").is_file()
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["license"] == "PolyForm-Shield-1.0.0"


def test_client_docs_stand_alone():
    """The client leaves this repository later: its READMEs must not lean on the server's files."""
    client = Path(__file__).resolve().parents[1]
    for readme in (client / "README.md", PLUGIN / "README.md"):
        text = readme.read_text()
        assert _prose_words(readme) >= 40
        for server_only in ("ledger_server", "packages/ledger-server", "ledger-server ",
                            "docs/architecture", "docs/operations"):
            assert server_only not in text, f"{readme} mentions {server_only!r}"


def test_every_relative_link_in_the_docs_resolves():
    import re
    for doc in [ROOT / "README.md", PLUGIN / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)#\s]+)", doc.read_text()):
            assert (doc.parent / target).exists(), f"{doc.name} links to missing {target}"


def test_the_runner_is_executable():
    assert os.access(RUNNER, os.X_OK)
