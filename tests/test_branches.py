"""Which branches are watched: `branches.watch` / `branches.ignore`, GitLab's pattern syntax."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from ledger import init as initial
from ledger.adapters.vcs.base import BranchRef, PullRef, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.config import Config
from ledger.core.branches import (DEFAULT_IGNORE, DEFAULT_RULES, BranchRules, matches,
                                  validate_branches)
from ledger.core.join import Change
from ledger.core.tickets import validate_keys
from ledger.pipeline.collect import collect

KEYS = validate_keys(["ABC"])
NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


class TestPatterns:
    @pytest.mark.parametrize("pattern,name,hit", [
        ("main", "main", True),
        ("main", "main-old", False),
        ("release/*", "release/1.2", True),
        ("release/*", "release/1.2/rc", True),       # `*` crosses `/`, as in GitLab
        ("release/*", "prerelease/1", False),
        ("*-stable", "14-stable", True),
        ("ABC-*", "abc-12-export", True),            # case does not matter
        ("feat.x", "featyx", False),                 # only `*` is special
    ])
    def test_gitlab_syntax(self, pattern, name, hit):
        assert matches(pattern, name) is hit

    def test_ignore_wins_over_watch(self):
        rules = BranchRules(watch=("feature/*",), ignore=("feature/wip-*",))
        assert rules.allows("feature/export")
        assert rules.ignored_by("feature/wip-export") == "feature/wip-*"
        assert rules.ignored_by("bugfix/x") == "not in watch"

    def test_defaults_cover_both_main_and_master_and_the_bots(self):
        for name in ("main", "master", "develop", "release/2.0", "dependabot/npm/lodash"):
            assert not DEFAULT_RULES.allows(name), name
        assert DEFAULT_RULES.allows("feat/abc-1-export")


class TestConfig:
    def test_absent_means_defaults(self):
        assert validate_branches(None) == DEFAULT_RULES

    def test_a_list_given_is_the_whole_list(self):
        rules = validate_branches({"ignore": ["wip/*"]})
        assert rules.ignore == ("wip/*",) and rules.watch == ("*",)
        assert rules.allows("main")      # the user replaced the defaults

    def test_a_single_string_is_one_pattern(self):
        assert validate_branches({"watch": "feature/*"}).watch == ("feature/*",)

    @pytest.mark.parametrize("raw", [["main"], {"watch": []}, {"watch": [""]},
                                     {"include": ["x"]}, {"ignore": [3]}])
    def test_mistakes_fail_at_load(self, raw):
        with pytest.raises(ValueError):
            validate_branches(raw)

    def test_loaded_from_the_file(self, tmp_path):
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n"
                     "branches:\n  watch: ['feature/*']\n  ignore: ['feature/wip-*']\n")
        cfg = Config.load(p)
        assert cfg.branches == BranchRules(("feature/*",), ("feature/wip-*",))

    def test_example_config_loads(self):
        assert Config.load("config/ledger.example.yml").branches.ignore == DEFAULT_IGNORE


R = "acme/orders"


def _adapter(default="master"):
    def b(name):
        return BranchRef(repo=R, name=name, head_sha="a" * 40, updated_at=NOW, author="a@x")
    return FakeAdapter(
        repos=[RepoRef(full_name=R, default_branch=default, host="github.com")],
        branches={R: [b("feat/abc-1-export"), b("wip/abc-2-try"), b("dependabot/npm/x"),
                      b(default), b("main")]},
        pulls={R: [PullRef(repo=R, number=7, title="ABC-3 bump", head_ref="renovate/lodash",
                           base_ref=default, head_sha="b" * 40, author="bot", updated_at=NOW)]},
    )


class TestCollect:
    def test_defaults_leave_out_bots_and_long_lived_lines(self):
        got = collect([_adapter()], KEYS, now=NOW)
        refs = {c.ref for c in got.changes}
        assert refs == {"feat/abc-1-export", "wip/abc-2-try"}
        assert got.branches_ignored == 3      # dependabot, renovate PR, a stray `main`

    def test_watch_narrows_and_ignore_applies_to_pull_requests_too(self):
        rules = BranchRules(watch=("feat/*", "renovate/*"), ignore=("renovate/*",))
        got = collect([_adapter()], KEYS, branches=rules, now=NOW)
        assert {c.ref for c in got.changes} == {"feat/abc-1-export"}

    def test_the_default_branch_is_never_a_change_even_when_watched(self):
        got = collect([_adapter("trunk")], KEYS, branches=BranchRules(("*",), ()), now=NOW)
        assert "trunk" not in {c.ref for c in got.changes}


class TestLiveLinking:
    def test_population_follows_the_config(self, tmp_path):
        from ledger.service import LiveIndex
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\nbranches:\n  ignore: ['wip/*']\n")
        cfg = Config.load(p)
        idx = LiveIndex(FakeAdapter(repos=[]), {})
        changes = [Change(repo=R, ref=r, tickets=()) for r in ("wip/a", "release/1", "feat/b")]
        idx.use_changes(changes, cfg)
        assert {c.ref for c in idx._population} == {"feat/b"}    # release/* is integration, never a change
        idx.use_changes(changes, None)
        assert {c.ref for c in idx._population} == {"wip/a", "feat/b"}


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


class TestInitWritesTheRulesOut:
    def test_defaults_written_and_prefixes_noted(self, tmp_path):
        repo = tmp_path / "orders"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "master")
        _git(repo, "-c", "user.email=a@x", "-c", "user.name=a", "commit", "-q",
             "--allow-empty", "-m", "start")
        for name in ("feature/abc-1", "feature/abc-2", "release/1.0", "snyk-fix-123"):
            _git(repo, "branch", name)
        got = initial.plan(repo)
        assert got.config["branches"]["watch"] == ["*"]
        assert got.config["branches"]["ignore"][:len(DEFAULT_IGNORE)] == list(DEFAULT_IGNORE)
        assert "snyk-*" in got.config["branches"]["ignore"]
        note = next(n for n in got.notes if "Branch names here" in n)
        assert "feature/* (2)" in note and "release/* (1, ignored)" in note
        # What init writes, Config reads back unchanged.
        got.config["project_keys"] = ["ABC"]
        p = tmp_path / "ledger.yml"
        p.write_text(got.render())
        assert Config.load(p).branches.ignore == tuple(got.config["branches"]["ignore"])


class TestSentToTheServer:
    def _cfg(self, tmp_path, body):
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n" + body)
        return Config.load(p)

    def test_rules_the_file_states_go_as_a_header(self, tmp_path):
        import json

        from ledger import remote
        got = remote.headers_for(self._cfg(tmp_path, "branches:\n  ignore: ['wip/*']\n"))
        assert json.loads(got[remote.BRANCHES_HEADER]) == {"watch": ["*"], "ignore": ["wip/*"]}

    def test_nothing_is_sent_when_the_file_says_nothing(self, tmp_path):
        from ledger import remote
        assert remote.headers_for(self._cfg(tmp_path, "")) == {}
        assert remote.headers_for(None) == {}


class TestReleaseBranchesAreIntegratedWork:
    def test_patterns_expand_to_live_branches(self):
        from ledger.core.branches import expand_integration
        got = expand_integration(("stage", "release/*"), ["release/1.4", "feat/x", "release/1.5"])
        assert got == ("stage", "release/1.4", "release/1.5")

    def test_a_ticket_going_out_in_a_release_still_links(self):
        from ledger.adapters.vcs.base import CommitRef
        a = _adapter()
        a._branches[R].append(BranchRef(repo=R, name="release/1.4", head_sha="c" * 40,
                                        updated_at=NOW, author="rm@x"))
        a._commits[(R, "release/1.4")] = [CommitRef(
            sha="d" * 40, subject="Merge ABC-3 totals", author="b@x", when=NOW,
            merged_ref="feat/abc-3-totals")]
        got = collect([a], KEYS, integration_branches=("release/*",), now=NOW)
        row = next(c for c in got.changes if c.ref == "feat/abc-3-totals")
        assert row.kind == "integration" and row.integration_ref == "release/1.4"
        assert "release/1.4" not in {c.ref for c in got.changes}

    def test_default_includes_release(self, tmp_path):
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        cfg = Config.load(p)
        assert "release/*" in cfg.integration_branches
        assert "release/*" not in cfg.integration_names


class TestBaseAndWorktreeReachTheServer:
    def test_remote_brief_sends_the_base(self, monkeypatch):
        from ledger.surfaces import hook, mcp
        seen = {}

        def fake_http(url, token, timeout, **kw):
            seen.update(kw)
            return lambda repo, ref, base=None: seen.update(base=base) or {}
        monkeypatch.setattr(hook, "http_brief", fake_http)
        mcp.RemoteBackend("http://x", None).brief("acme/orders", "feat/a", base="stage")
        assert seen["base"] == "stage" and seen["via"] == "mcp"

    def test_worktree_sends_this_machines_change(self, monkeypatch, tmp_path):
        from ledger import remote, service
        from ledger.surfaces import mcp

        class Wire:
            def to_wire(self):
                return {"base": "stage", "files": []}
        seen = {}
        monkeypatch.setattr(service, "build_adapters", lambda cfg, root: [])
        monkeypatch.setattr(service, "tree_reader", lambda adapters: None)
        monkeypatch.setattr(service, "read_local_change",
                            lambda cfg, vcs, repo, ref, worktree, base: seen.update(base=base) or Wire())
        monkeypatch.setattr(remote, "post", lambda url, tok, path, body, **kw: seen.update(
            path=path, body=body, timeout=kw.get("timeout")) or {})
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        mcp.RemoteBackend("http://ledger.example", None, cfg=Config.load(p)).brief(
            "acme/orders", "feat/a", base="stage", worktree=True)
        assert seen["path"] == "/brief" and seen["base"] == "stage"
        assert seen["body"]["local"] == {"base": "stage", "files": []}
        assert seen["timeout"] == remote.BRIEF_TIMEOUT

    def test_a_server_on_this_machine_reads_the_worktree_itself(self, monkeypatch, tmp_path):
        from ledger.surfaces import hook, mcp
        seen = {}

        def fake_http(url, token, timeout, **kw):
            seen.update(kw)
            return lambda repo, ref, base=None: {}
        monkeypatch.setattr(hook, "http_brief", fake_http)
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        mcp.RemoteBackend("http://localhost:8765", None, cfg=Config.load(p)).brief(
            "acme/orders", "feat/a", worktree=True)
        assert seen["worktree"] is True

    def test_cli_brief_takes_base(self):
        from typer.testing import CliRunner

        from ledger.cli import app
        import click
        out = CliRunner().invoke(app, ["brief", "--help"], env={"COLUMNS": "200"})
        assert "--base" in click.unstyle(out.output)     # CI forces colour; option names come styled


class TestIntegrationBranchesAreNeverChanges:
    def _a(self, pulls):
        from ledger.adapters.vcs.base import PullRef
        a = _adapter("main")
        a._pulls = {R: [PullRef(repo=R, number=n, title="ABC-9 x", head_ref=h, base_ref="main",
                                head_sha="e" * 40, author="u", updated_at=NOW, fork=f)
                        for n, (h, f) in enumerate(pulls)]}
        return a

    def test_a_release_train_pull_request_is_not_a_change_with_custom_rules(self):
        got = collect([self._a([("develop", False)])], KEYS,
                      integration_branches=("develop",), branches=BranchRules(("*",), ()), now=NOW)
        assert "develop" not in {c.ref for c in got.changes}

    def test_a_fork_pull_request_from_its_main_is_a_change(self):
        got = collect([self._a([("main", True)])], KEYS, now=NOW)
        assert ("main", "pr") in {(c.ref, c.kind) for c in got.changes}

    def test_an_ignored_pull_request_counts_once(self):
        a = self._a([("dependabot/npm/x", False)])
        got = collect([a], KEYS, now=NOW)
        assert got.branches_ignored == 1     # the PR's branch once; `main` is integration, not ignored

    def test_the_branch_being_briefed_is_kept(self):
        got = collect([_adapter()], KEYS, branches=BranchRules(("feat/*",), ()),
                      keep=(R, "wip/abc-2-try"), now=NOW)
        assert "wip/abc-2-try" in {c.ref for c in got.changes}

    def test_live_linking_leaves_integration_branches_out_with_custom_rules(self, tmp_path):
        from ledger.service import LiveIndex
        p = tmp_path / "ledger.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: [develop, 'release/*']\n"
                     "branches:\n  ignore: ['wip/*']\n")
        idx = LiveIndex(FakeAdapter(repos=[]), {})
        idx.use_changes([Change(repo=R, ref=r, tickets=()) for r in ("develop", "release/2", "feat/b")],
                        Config.load(p))
        assert {c.ref for c in idx._population} == {"feat/b"}
