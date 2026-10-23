"""Replay, against real git history: two repositories, real merge commits."""

from __future__ import annotations

import os
import subprocess
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from ledger import replay as rp
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.cli import app
from ledger.config import Config

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "ci@example.com",
       "PATH": os.environ["PATH"]}


def git(path, *args, email="ann@example.com"):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**ENV, "GIT_AUTHOR_EMAIL": email, "HOME": str(path)})


def repo(root, name, files):
    p = root / name
    p.mkdir(parents=True)
    git(p, "init", "-q", "-b", "main")
    git(p, "remote", "add", "origin", f"git@github.com:acme/{name}.git")
    for f, text in files.items():
        (p / f).parent.mkdir(parents=True, exist_ok=True)
        (p / f).write_text(text)
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "start")
    return p


def merge(p, branch, files, *, pr, email="ann@example.com"):
    """A branch with one commit, merged the way a forge words it."""
    git(p, "checkout", "-q", "-b", branch)
    for f, text in files.items():
        (p / f).parent.mkdir(parents=True, exist_ok=True)
        (p / f).write_text(text)
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", f"work on {branch}", email=email)
    git(p, "checkout", "-q", "main")
    git(p, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{pr} from acme/{branch}")
    git(p, "branch", "-q", "-D", branch)


@pytest.fixture
def estate(tmp_path):
    root = tmp_path / "src"
    orders = repo(root, "orders", {"app/models.py": "__tablename__ = 'order_line'\n"})
    billing = repo(root, "billing", {"app/db.py": "__tablename__ = 'order_line'\n"})
    # Two halves of one ticket, in two repositories.
    merge(orders, "feat/abc-7-read-lines", {"app/read.py": "x = 1\n"}, pr=1)
    merge(billing, "feat/abc-7-split-lines", {"app/split.py": "y = 1\n"}, pr=2, email="bob@example.com")
    # Touches a table the other repository also has, with no ticket at all.
    merge(orders, "tidy-order-lines",
          {"app/archive.py": "class Archived:\n    __tablename__ = 'order_line'\n"}, pr=3)
    # Touches nothing anybody else has.
    merge(orders, "feat/abc-9-readme", {"README.md": "hello\n"}, pr=4)
    # A release train merging forward is not a change.
    merge(orders, "stage", {"VERSION": "2\n"}, pr=5)
    cfgp = tmp_path / "ledger.yml"
    cfgp.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: []\n")
    return Config.load(str(cfgp)), root


def by_ref(got):
    return {c.change.ref: c for c in got.cases}


SINCE = date.today() - timedelta(days=7)


class TestAChangeIsBriefedAsItWas:
    def test_work_in_flight_that_day_in_another_repository_is_found(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        b = by_ref(got)["feat/abc-7-read-lines"].brief
        assert b["recommendation"] == "review_with"
        assert [(i["repo"], i["ref"]) for i in b["impacts"]] == [("billing", "feat/abc-7-split-lines")]

    def test_its_own_diff_is_read_though_it_merged_long_ago(self, estate):
        cfg, root = estate
        b = by_ref(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))["tidy-order-lines"].brief
        # No ticket links it; the table it touches does.
        assert b["recommendation"] == "review_with"
        assert [c["name"] for c in b["contracts"] if c["also_touched_by"]] == ["order_line"]
        assert b["subject"]["ref"] == "tidy-order-lines" and b["subject"]["own_commits"] == 1

    def test_a_change_with_nothing_to_say_is_silent(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        assert by_ref(got)["feat/abc-9-readme"].brief["recommendation"] == "proceed"
        assert {c.change.ref for c in got.with_context} == {
            "feat/abc-7-read-lines", "feat/abc-7-split-lines", "tidy-order-lines"}

    def test_a_release_train_is_not_a_change(self, estate):
        cfg, root = estate
        assert "stage" not in by_ref(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))

    def test_mine_means_who_wrote_the_branch_not_who_merged_it(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author="bob@example.com")
        assert [c.change.ref for c in got.cases] == ["feat/abc-7-split-lines"]
        # Somebody else's half is still in flight around it.
        assert got.cases[0].brief["impacts"][0]["repo"] == "orders"

    def test_the_limit_takes_the_newest(self, estate):
        cfg, root = estate
        assert len(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None, limit=2).cases) == 2


class TestTheReport:
    def test_it_states_the_funnel_and_asks_the_question_per_case(self, estate):
        cfg, root = estate
        text = rp.render(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))
        assert "   4 changes replayed" in text and "   3 with cross-repository context" in text
        assert text.count("Would knowing this have changed what you did?") == 3
        assert "[ ] prevented a defect" in text and "[ ] noise" in text
        assert "- [ ] missed · " in text and "feat/abc-9-readme" in text

    def test_it_says_what_was_read_from_today(self, estate):
        cfg, root = estate
        text = rp.render(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))
        assert "read from today's" in text and "upper bound" in text

    def test_a_live_replay_ends_with_what_a_server_adds(self, estate):
        from ledger import upgrade
        cfg, root = estate
        text = rp.render(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))
        section = text[text.index("## What an organisation server adds"):]
        assert upgrade.WHERE in section and "33 of 35" in section
        assert all(a in section for a in upgrade.ADDS)

    def test_a_replay_against_the_index_does_not_advertise_it(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        got.source = "stored"
        assert "What an organisation server adds" not in rp.render(got)

    def test_an_estate_with_no_merge_commits_says_why_it_is_empty(self, tmp_path):
        repo(tmp_path / "src", "orders", {"a.py": "x\n"})
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        text = rp.render(rp.run(Config.load(str(p)), LocalGitAdapter(str(tmp_path / "src")),
                                since=SINCE, author=None))
        assert "squash-merges everything" in text


class TestTheCommand:
    def test_it_writes_the_report(self, estate, tmp_path):
        cfg, root = estate
        out = tmp_path / "report.md"
        res = CliRunner().invoke(app, ["replay", "--config", str(tmp_path / "ledger.yml"),
                                       "--local-root", str(root), "--everyone", "--days", "7",
                                       "--out", str(out)])
        assert res.exit_code == 0, res.output
        assert "replayed 4 change(s); 3 had" in res.output and out.read_text().startswith("# Ledger replay")
