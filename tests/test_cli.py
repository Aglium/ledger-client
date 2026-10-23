"""CLI-level tests for the stateless path the GitHub Action uses."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ledger import cli as cli_mod
from ledger.adapters.vcs.base import BranchRef, PullRef, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter

runner = CliRunner()


@pytest.fixture(autouse=True)
def test_config(tmp_path, monkeypatch):
    """The CLI tests used to pass only because a real deployment's config was
    committed to the repo. They carry their own now."""
    cfg = tmp_path / "ledger.yml"
    cfg.write_text(
        "org: test\n"
        "project_keys: [ABC, XYZ]\n"
        "liveness_days: 30\n"
        "integration_branches: [stage]\n"
        "forges: []\n"
        "model: {kind: fake}\n"
    )
    monkeypatch.setenv("LEDGER_CONFIG", str(cfg))
    return cfg
LEADS, ORCH = "intake-service", "orchestrator"


@pytest.fixture
def fake_forge(monkeypatch):
    a = FakeAdapter(
        repos=[RepoRef(full_name=LEADS, default_branch="main", host="github.com"),
               RepoRef(full_name=ORCH, default_branch="master", host="github.com")],
        branches={ORCH: [BranchRef(repo=ORCH, name="test/abc-321-pin-the-intake-contract", head_sha="b" * 40)]},
        pulls={LEADS: [PullRef(repo=LEADS, number=12, title="ABC-321 export attribution",
                               head_ref="feat/abc-321-export", base_ref="main", head_sha="a" * 40,
                               author="alice", url="https://x/12")]},
    )
    monkeypatch.setattr(cli_mod, "_adapters", lambda cfg, local_root: [a])
    return a


class TestCommentLive:
    def test_renders_siblings_without_a_database(self, fake_forge):
        r = runner.invoke(cli_mod.app, ["comment", LEADS, "12", "--ref", "feat/abc-321-export", "--live"])
        assert r.exit_code == 0, r.output
        assert "orchestrator" in r.output and "ABC-321" in r.output

    def test_posts_when_asked(self, fake_forge):
        r = runner.invoke(cli_mod.app,
                          ["comment", LEADS, "12", "--ref", "feat/abc-321-export", "--live", "--post"])
        assert r.exit_code == 0, r.output
        assert fake_forge.comments, "the comment should have been written to the forge"
        assert "orchestrator" in next(iter(fake_forge.comments.values()))

    def test_second_post_updates_rather_than_appends(self, fake_forge):
        for _ in range(2):
            runner.invoke(cli_mod.app,
                          ["comment", LEADS, "12", "--ref", "feat/abc-321-export", "--live", "--post"])
        assert len(fake_forge.comments) == 1

    def test_silent_when_branch_has_no_ticket(self, fake_forge):
        r = runner.invoke(cli_mod.app, ["comment", LEADS, "12", "--ref", "feat/config-menu", "--live"])
        assert r.exit_code == 0
        assert "nothing to join on" in r.output
        assert not fake_forge.comments

    def test_silent_when_ticket_has_no_siblings(self, fake_forge):
        r = runner.invoke(cli_mod.app, ["comment", LEADS, "12", "--ref", "feat/abc-999-lonely", "--live"])
        assert r.exit_code == 0
        assert "no comment" in r.output
        assert not fake_forge.comments

    def test_live_needs_no_db_url(self, fake_forge, monkeypatch):
        """The Action has no database. Requiring one would make it un-adoptable."""
        monkeypatch.delenv("LEDGER_DB_URL", raising=False)
        r = runner.invoke(cli_mod.app, ["comment", LEADS, "12", "--ref", "feat/abc-321-export", "--live"])
        assert r.exit_code == 0, r.output


class TestCheckRun:
    SHA = "a" * 40

    def _post(self, ref, fake_forge, **kw):
        return runner.invoke(cli_mod.app, [
            "comment", LEADS, "12", "--ref", ref, "--live", "--post", "--head-sha", self.SHA,
        ])

    def test_siblings_produce_a_neutral_check_not_a_failure(self, fake_forge):
        """Advisory. A tool that red-builds a branch on a hunch gets removed."""
        r = self._post("feat/abc-321-export", fake_forge)
        assert r.exit_code == 0, r.output
        (conclusion, title, _), = fake_forge.checks.values()
        assert conclusion == "neutral"
        assert "ABC-321" in title

    def test_clean_scan_produces_success(self, fake_forge):
        r = self._post("feat/abc-999-lonely", fake_forge)
        assert r.exit_code == 0, r.output
        (conclusion, title, _), = fake_forge.checks.values()
        assert conclusion == "success" and "No cross-repo siblings" in title

    def test_check_is_written_even_when_no_comment_is(self, fake_forge):
        """A PR the index examined and found clean deserves to say so."""
        self._post("feat/abc-999-lonely", fake_forge)
        assert fake_forge.checks and not fake_forge.comments

    def test_rerun_updates_the_same_check(self, fake_forge):
        for _ in range(3):
            self._post("feat/abc-321-export", fake_forge)
        assert len(fake_forge.checks) == 1

    def test_no_head_sha_warns_and_still_comments(self, fake_forge):
        r = runner.invoke(cli_mod.app,
                          ["comment", LEADS, "12", "--ref", "feat/abc-321-export", "--live", "--post"])
        assert r.exit_code == 0, r.output
        assert not fake_forge.checks
        assert fake_forge.comments, "a missing --head-sha must not suppress the comment"

    def test_dry_run_reports_the_verdict_and_writes_nothing(self, fake_forge):
        r = runner.invoke(cli_mod.app, [
            "comment", LEADS, "12", "--ref", "feat/abc-321-export", "--live", "--head-sha", self.SHA,
        ])
        assert r.exit_code == 0
        assert "check: neutral" in r.output
        assert not fake_forge.checks and not fake_forge.comments


class TestSubjectGuard:
    """An unknown repo or ref is an input error, not a verdict. The original
    bug: repo and branch glued into one argument by a shell loop produced
    PROCEED at high confidence."""

    @pytest.fixture
    def estate(self, monkeypatch):
        a = FakeAdapter(
            repos=[RepoRef(full_name=LEADS, default_branch="main", host="local"),
                   RepoRef(full_name=ORCH, default_branch="main", host="local")],
            tree={LEADS: {"README.md": "# leads"}, ORCH: {"README.md": "# orch"}},
            ahead={(LEADS, "main", "feat/abc-321-brazil"): 0,
                   (LEADS, "stage", "feat/abc-321-brazil"): 0},
        )
        monkeypatch.setattr(cli_mod, "_adapters", lambda cfg, local_root: [a])
        return a

    @pytest.mark.parametrize("cmd", ["brief", "explain"])
    def test_unknown_repo_is_a_clean_error_not_a_verdict(self, estate, cmd):
        r = runner.invoke(cli_mod.app, [cmd, f"{LEADS} feat/abc-321-brazil", ""])
        assert r.exit_code == 2
        assert "unknown repo" in r.output
        assert "PROCEED" not in r.output.upper()

    def test_close_matches_are_suggested(self, estate):
        r = runner.invoke(cli_mod.app, ["brief", "intake-servce", "feat/abc-321-brazil"])
        assert r.exit_code == 2 and LEADS in r.output

    @pytest.mark.parametrize("cmd", ["brief", "explain"])
    def test_unknown_ref_is_a_clean_error(self, estate, cmd):
        r = runner.invoke(cli_mod.app, [cmd, LEADS, "no/such/branch"])
        assert r.exit_code == 2
        assert "unknown ref" in r.output

    def test_a_fully_integrated_branch_reports_its_base(self, estate):
        r = runner.invoke(cli_mod.app, ["brief", LEADS, "feat/abc-321-brazil", "--json"])
        assert r.exit_code == 0, r.output
        import json
        subject = json.loads(r.output)["subject"]
        assert subject["resolved"] is True and subject["own_commits"] == 0
