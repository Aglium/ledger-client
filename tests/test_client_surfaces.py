"""The client's own answers: read live, with no server and no database."""

from __future__ import annotations

import asyncio
import json

import pytest

from ledger import remote
from ledger.adapters.vcs.base import BranchRef, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.config import Config
from ledger.service import LedgerError, Unavailable
from ledger.surfaces import hook as h
from ledger.surfaces.mcp import LIVE_NOTE, LiveBackend, build_server

ADR = """---
id: "0001"
title: The order_line table is owned by billing
status: accepted
ticket: ABC-1
deciders: [Alice Example]
---
# ADR 1
The remaining gate is that order_line still lacks a backfill for old rows.
"""
TREE = {
    "billing": {"app/db.py": "__tablename__ = 'order_line'\n", "docs/adr/0001-order-line.md": ADR},
    "orders": {"app/models.py": "__tablename__ = 'order_line'\n"},
    "catalog": {"app/models.py": "__tablename__ = 'product'\n"},
}


def estate():
    branches = {r: [BranchRef(repo=r, name="main", head_sha="a" * 40)] for r in TREE}
    branches["orders"].append(BranchRef(repo="orders", name="feat/abc-7-read-lines", head_sha="b" * 40))
    branches["billing"].append(BranchRef(repo="billing", name="feat/abc-7-split-lines", head_sha="c" * 40))
    return FakeAdapter(
        repos=[RepoRef(full_name=r, default_branch="main", host="fake") for r in TREE],
        branches=branches, tree={r: dict(files) for r, files in TREE.items()})


@pytest.fixture
def backend(tmp_path):
    p = tmp_path / "ledger.yml"
    p.write_text("org: testorg\nproject_keys: [ABC]\nintegration_branches: []\n")
    vcs = estate()
    return LiveBackend(Config.load(str(p)), lambda: [vcs])


class TestLiveBackend:
    def test_who_owns_names_the_repositories_and_says_what_it_cannot_know(self, backend):
        got = backend.who_owns("table", "order_line")
        assert [r["repo"] for r in got["repos"]] == ["billing", "orders"]
        # Read live there is no direction: naming a table is not owning it.
        assert {r["uses"] for r in got["repos"]} == {"mentions"}
        assert LIVE_NOTE in got["notes"] and got["index"]["source"] == "live"

    def test_an_unknown_contract_is_not_found_rather_than_unused(self, backend):
        got = backend.who_owns("topic", "nobody.v1")
        assert got["repos"] == [] and "NOT FOUND" in got["notes"][0]

    def test_an_unknown_surface_is_an_input_error(self, backend):
        with pytest.raises(LedgerError):
            backend.who_owns("queue", "x")

    def test_inflight_is_collected_for_this_call(self, backend):
        got = backend.inflight_for("abc-7")
        assert got["repos"] == ["billing", "orders"]
        assert got["freshness"]["synced_at"] is None

    def test_decisions_are_read_from_the_default_branches(self, backend):
        got = backend.decisions_for(None, "order_line", None)
        assert [d["title"] for d in got["decisions"]] == ["The order_line table is owned by billing"]
        assert got["decisions"][0]["deciders"] == ["Alice Example"]

    def test_decisions_need_something_to_match(self, backend):
        with pytest.raises(LedgerError):
            backend.decisions_for(None, None, None)

    def test_feedback_says_there_is_nowhere_to_keep_it(self, backend):
        got = backend.feedback("useful", "found the other writer", None, None, None, None)
        assert got["kept"] is False and "organisation server" in got["note"]
        # Nothing is counted or sent anywhere: the client has no usage reporting.
        assert "counted_anonymously" not in got

    def test_a_verdict_that_is_not_one_is_refused(self, backend):
        with pytest.raises(LedgerError, match="verdict"):
            backend.feedback("fine", "", None, None, None, None)


class TestMcp:
    def call(self, srv, name, args):
        res = asyncio.run(srv.call_tool(name, args))
        blocks = res[0] if isinstance(res, tuple) else getattr(res, "content", res)
        return json.loads(blocks[0].text)

    def test_the_same_five_tools_with_no_server(self, backend):
        srv = build_server(backend)
        assert {t.name for t in asyncio.run(srv.list_tools())} == {
            "brief", "inflight_for", "who_owns", "decisions_for", "feedback"}

    def test_an_error_arrives_as_data(self, backend):
        got = self.call(build_server(backend), "who_owns", {"surface": "queue", "name": "x"})
        assert "unknown surface" in got["error"]


class TestRemote:
    def test_an_unreachable_server_is_unavailable_not_a_traceback(self):
        with pytest.raises(Unavailable):
            remote.get("http://127.0.0.1:9", None, "/brief", {"repo": "a", "ref": "b"}, timeout=1)


class TestSessionStartIsAnsweredOnce:
    def test_the_second_ask_within_the_window_is_a_duplicate(self, tmp_path):
        assert h.answered_recently("session_start", "orders", "main", stamps=tmp_path) is False
        assert h.answered_recently("session_start", "orders", "main", stamps=tmp_path) is True

    def test_another_branch_is_another_question(self, tmp_path):
        h.answered_recently("session_start", "orders", "main", stamps=tmp_path)
        assert h.answered_recently("session_start", "orders", "feat/x", stamps=tmp_path) is False

    def test_outside_the_window_it_is_asked_again(self, tmp_path):
        h.answered_recently("session_start", "orders", "main", stamps=tmp_path)
        assert h.answered_recently("session_start", "orders", "main", stamps=tmp_path,
                                   within_seconds=0) is False


class TestADeveloperCanFindOutAboutTheServer:
    def test_the_command_says_what_it_adds_where_to_ask_and_how_to_connect(self, monkeypatch):
        from typer.testing import CliRunner

        from ledger import upgrade
        from ledger.cli import app
        monkeypatch.delenv("LEDGER_URL", raising=False)
        out = CliRunner().invoke(app, ["server"]).output
        assert upgrade.WHERE in out and "LEDGER_URL" in out and "LEDGER_API_TOKEN" in out
        assert "1 to 3 seconds" in out and upgrade.COMPARE in out

    def test_connected_it_says_so_instead(self, monkeypatch):
        from typer.testing import CliRunner

        from ledger.cli import app
        monkeypatch.setenv("LEDGER_URL", "https://ledger.acme.example")
        out = CliRunner().invoke(app, ["server"]).output
        assert "connected to an organisation server: https://ledger.acme.example" in out

    def test_a_live_answer_points_at_it(self, backend):
        assert "aglium-ledger server" in backend.who_owns("table", "order_line")["notes"][0]
