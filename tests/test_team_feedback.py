"""What a team trying 0.1.1 reported, each as a rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import click
from typer.testing import CliRunner

from ledger.adapters.vcs.base import RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.cli import app
from ledger.config import Config
from ledger.core.join import Change
from ledger.core.rank import items_of, rank
from ledger.service import LiveIndex, resolve_subject

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


class TestRelatedByRepositoryDoesNotLead:
    def test_four_branches_of_one_repository_are_not_findings(self):
        b = {"impacts": [
            {"repo": "acme/core", "ref": f"feat/x{n}", "kind": "branch",
             "relation": "its repository publishes topic `t`, which this change touches, but this "
                         "branch's diff does not name it — related by repository",
             "how": "both change topic `t`"} for n in range(4)]}
        got = rank(items_of(b))
        assert got.lead == [] and got.also["count"] == 4

    def test_a_shared_name_still_leads(self):
        b = {"impacts": [{"repo": "acme/core", "ref": "feat/x", "kind": "branch",
                          "relation": "it produces → you consume topic `t`"}]}
        assert len(rank(items_of(b)).lead) == 1


class TestSameAuthorNeedsASharedWord:
    def _index(self, refs):
        idx = LiveIndex(FakeAdapter(repos=[]), {})
        idx.use_changes([Change(repo="acme/web", ref=r, tickets=(), author="a@x",
                                updated_at=NOW + timedelta(minutes=10)) for r in refs])
        return idx

    def test_time_alone_links_nothing(self):
        got = self._index(["feat/dashboards"]).siblings_near(
            author="a@x", at=NOW, ref="feat/voice-bot-greeting", exclude_repo="acme/bot")
        assert got == []

    def test_a_shared_word_links(self):
        got = self._index(["feat/greeting-api"]).siblings_near(
            author="a@x", at=NOW, ref="feat/voice-bot-greeting", exclude_repo="acme/bot")
        assert [s.change.ref for s in got] == ["feat/greeting-api"]


class _Ahead(FakeAdapter):
    def __init__(self, ahead, merged=None, branches=()):
        from ledger.adapters.vcs.base import BranchRef
        super().__init__(repos=[RepoRef(full_name="acme/orders", default_branch="main",
                                        host="local")],
                         branches={"acme/orders": [BranchRef(repo="acme/orders", name=n,
                                                             head_sha="a" * 40, updated_at=NOW)
                                                   for n in branches]})
        self._a = ahead
        self.merged = merged or {}

    def commits_ahead(self, repo, base, head):
        if base not in self._a:
            from ledger.adapters.vcs.base import ForgeUnavailable
            raise ForgeUnavailable("fake", base)
        return self._a[base]


class TestMergedIntoStageNotIntoMain:
    def _cfg(self, tmp_path, integration="[stage]"):
        p = tmp_path / "ledger.yml"
        p.write_text(f"org: acme\nproject_keys: [ABC]\nintegration_branches: {integration}\n")
        return Config.load(p)

    def test_read_against_main_and_said(self, tmp_path):
        vcs = _Ahead({"main": 3, "stage": 0}, merged={("acme/orders", "stage", "feat/a"): True})
        s = resolve_subject(self._cfg(tmp_path), vcs, "acme/orders", "feat/a")
        assert (s.base, s.own_commits, s.integrated_in) == ("main", 3, "stage")

    def test_a_branch_just_cut_from_stage_is_not_merged_there(self, tmp_path):
        """`git checkout -b feat/x stage`: nothing of its own yet. Read against
        main it would be credited with the whole release train."""
        vcs = _Ahead({"main": 300, "stage": 0}, merged={("acme/orders", "stage", "feat/x"): False})
        s = resolve_subject(self._cfg(tmp_path), vcs, "acme/orders", "feat/x")
        assert (s.base, s.own_commits, s.integrated_in) == ("stage", 0, None)

    def test_a_source_that_cannot_tell_reads_it_as_before(self, tmp_path):
        s = resolve_subject(self._cfg(tmp_path), _Ahead({"main": 3, "stage": 0}),
                            "acme/orders", "feat/a")
        assert (s.base, s.integrated_in) == ("stage", None)

    def test_the_integration_branch_itself_is_not_merged_into_itself(self, tmp_path):
        vcs = _Ahead({"main": 300, "stage": 0}, merged={("acme/orders", "stage", "stage"): True})
        s = resolve_subject(self._cfg(tmp_path), vcs, "acme/orders", "stage")
        assert s.integrated_in is None

    def test_in_main_too_is_simply_integrated(self, tmp_path):
        s = resolve_subject(self._cfg(tmp_path), _Ahead({"main": 0, "stage": 0}),
                            "acme/orders", "feat/a")
        assert (s.base, s.own_commits, s.integrated_in) == ("main", 0, None)

    def test_an_explicit_base_wins(self, tmp_path):
        s = resolve_subject(self._cfg(tmp_path), _Ahead({"main": 3, "stage": 0}),
                            "acme/orders", "feat/a", prefer="stage")
        assert (s.base, s.own_commits) == ("stage", 0)

    def test_a_release_branch_is_a_base_too(self, tmp_path):
        vcs = _Ahead({"main": 2, "release/2.4": 0}, branches=["release/2.4", "feat/y"],
                     merged={("acme/orders", "release/2.4", "feat/y"): True})
        s = resolve_subject(self._cfg(tmp_path, '["release/*"]'), vcs, "acme/orders", "feat/y")
        assert (s.base, s.integrated_in) == ("main", "release/2.4")

    def test_the_brief_says_so(self):
        from ledger.core.brief import build_brief
        from ledger.core.join import JoinResult
        b = build_brief(JoinResult(subject=Change(repo="acme/orders", ref="feat/a", tickets=())),
                        [], base="main", own_commits=3, integrated_in="stage")
        assert any("already merged into `stage`" in r for r in b.reasons)

    def test_it_survives_the_trip_to_the_server(self):
        from ledger.service import LocalChange
        wire = LocalChange("main", 3, integrated_in="stage").to_wire()
        assert LocalChange.from_wire(wire).integrated_in == "stage"
        assert LocalChange.from_wire(LocalChange("main", 3).to_wire()).integrated_in is None

    def test_decisions_on_a_release_branch_count_as_integrated(self):
        from ledger.core.branches import Patterns
        assert "release/2.4" in Patterns(["stage", "release/*"])
        assert "feat/x" not in Patterns(["stage", "release/*"])


class TestAsk:
    def test_commands_exist(self):
        out = CliRunner().invoke(app, ["ask", "--help"], env={"COLUMNS": "200"})
        for cmd in ("inflight", "owners", "decisions"):
            assert cmd in click.unstyle(out.output)

    def test_decisions_needs_something_to_match(self):
        assert CliRunner().invoke(app, ["ask", "decisions"]).exit_code == 2

    def _server(self, monkeypatch):
        from ledger import remote
        seen = {}
        monkeypatch.setattr(remote, "get", lambda url, tok, path, params, **kw: seen.update(
            path=path, params=params, headers=kw.get("headers")) or {"changes": [], "path": path})
        return seen

    def test_asks_the_server_as_the_cli(self, monkeypatch, tmp_path):
        import json
        seen = self._server(monkeypatch)
        cfgp = tmp_path / "ledger.yml"
        cfgp.write_text("org: acme\nproject_keys: [ABC]\nbranches:\n  ignore: ['wip/*']\n")
        out = CliRunner().invoke(app, ["ask", "owners", "topic", "order.events", "--config", str(cfgp)],
                                 env={"LEDGER_URL": "http://ledger.example"})
        assert out.exit_code == 0, out.output
        assert json.loads(out.output)["path"] == "/owners"
        assert seen["params"]["via"] == "cli" and seen["params"]["name"] == "order.events"
        assert "X-Ledger-Branches" in seen["headers"]

    def test_a_broken_config_still_asks_the_server(self, monkeypatch, tmp_path):
        seen = self._server(monkeypatch)
        cfgp = tmp_path / "ledger.yml"
        cfgp.write_text("org: acme\nproject_keys: [ABC]\nbranches:\n  watch: []\n")
        out = CliRunner().invoke(app, ["ask", "inflight", "ABC-1", "--config", str(cfgp)],
                                 env={"LEDGER_URL": "http://ledger.example"})
        assert out.exit_code == 0 and seen["path"] == "/inflight" and not seen["headers"]

    def test_a_broken_config_without_a_server_is_a_clean_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LEDGER_URL", raising=False)
        cfgp = tmp_path / "ledger.yml"
        cfgp.write_text("org: acme\nproject_keys: [ABC]\nbranches:\n  watch: []\n")
        out = CliRunner().invoke(app, ["ask", "inflight", "ABC-1", "--config", str(cfgp)])
        assert out.exit_code == 2 and "not valid" in out.output
        assert out.exception is None or isinstance(out.exception, SystemExit)

    def test_live_answers_are_json(self, tmp_path, monkeypatch):
        import json

        from ledger import cli
        monkeypatch.delenv("LEDGER_URL", raising=False)
        cfgp = tmp_path / "ledger.yml"
        cfgp.write_text("org: acme\nproject_keys: [ABC]\n")
        monkeypatch.setattr(cli, "_adapters", lambda cfg, root: [FakeAdapter(repos=[])])
        out = CliRunner().invoke(app, ["ask", "inflight", "ABC-1", "--config", str(cfgp)])
        assert out.exit_code == 0, out.output
        assert "changes" in json.loads(out.output)


class TestNoTimeNoWord:
    def test_without_a_ref_nothing_links(self):
        idx = LiveIndex(FakeAdapter(repos=[]), {})
        idx.use_changes([Change(repo="acme/web", ref="feat/a", tickets=(), author="a@x",
                                updated_at=NOW)])
        assert idx.siblings_near(author="a@x", at=NOW, ref="", exclude_repo="x") == []


class TestByRepositoryIsAField:
    def test_the_flag_decides(self):
        b = {"impacts": [{"repo": "acme/core", "ref": "feat/x", "kind": "branch",
                          "relation": "anything at all", "by_repository": True}]}
        assert rank(items_of(b)).lead == []

    def test_the_old_sentence_still_matches_the_fallback(self):
        from ledger.core import rank as r
        from ledger.core.effect import delta_of, relation_line
        line = relation_line(delta_of("acme/core", []), {}, repo_role="publishes topic `t`")[0]
        assert line.startswith(r._BY_REPOSITORY)


class TestFeedbackKeepsTheEvent:
    def test_event_is_stored_here(self, tmp_path, monkeypatch):
        from ledger import feedback as fb
        monkeypatch.delenv("LEDGER_URL", raising=False)
        monkeypatch.setattr(fb, "store", lambda: tmp_path / "fb.jsonl")
        out = CliRunner().invoke(app, ["feedback", "record", "useful", "--event", "e123"])
        assert out.exit_code == 0, out.output
        assert fb.rows()[-1]["event_id"] == "e123"
