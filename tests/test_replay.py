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
    def test_it_states_the_result_and_asks_for_a_verdict_per_finding(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        text = rp.render(got)
        assert "   4 changes replayed" in text
        assert "   3 with something to say about another repository" in text
        assert "   1 where nothing was found, with enough coverage to say so" in text
        assert text.count("Your verdict (`") == 3
        assert "[ ] useful" in text and "[ ] already known" in text
        assert "[ ] false positive" in text and "[ ] can't verify" in text
        readme = by_ref(got)["feat/abc-9-readme"]
        assert f"- [ ] missed (`{readme.change.id}`)" in text

    def test_it_says_it_read_the_estate_as_it_was(self, estate):
        cfg, root = estate
        text = rp.render(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))
        assert "No hindsight" in text and "upper bound" in text
        assert "read from the organisation index as it is today" not in text

    def test_a_replay_against_the_index_says_it_is_today(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        got.source = "stored"
        text = rp.render(got)
        assert "as it is today" in text and "No hindsight" not in text

    def test_it_names_the_repositories_it_read(self, estate):
        cfg, root = estate
        text = rp.render(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None))
        assert "- 2 repositories read: billing, orders." in text
        assert "**Gap:**" not in text

    def test_every_finding_links_to_evidence_a_reader_can_open(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        case = by_ref(got)["feat/abc-7-read-lines"]
        kinds = [e["kind"] for e in case.evidence]
        assert kinds[0] == "change" and "in_flight" in kinds
        assert case.evidence[0]["link"] == f"https://github.com/acme/orders/commit/{case.change.head_sha}"
        flight = next(e for e in case.evidence if e["kind"] == "in_flight")
        assert flight["link"].startswith("https://github.com/acme/billing/commit/")
        table = by_ref(got)["tidy-order-lines"]
        shared = next(e for e in table.evidence if e["kind"] == "contract")
        assert "order_line" in shared["text"] and "app/db.py" in shared["text"]
        assert "/blob/" in shared["link"] and shared["link"].endswith("/app/db.py")
        text = rp.render(got)
        assert "Evidence, as of " in text

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
        assert "## Nothing to replay" in text and "No merged pull requests were found" in text
        assert "**Gap:** Ledger can see 1 repository" in text

    def test_none_of_mine_says_so_and_how_to_see_the_team(self, estate):
        cfg, root = estate
        text = rp.render(rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE,
                                author="nobody@example.com"))
        assert "none of them written by nobody@example.com" in text and "--everyone" in text


class TestStrength:
    def test_a_shared_ticket_alone_is_called_correlation(self, estate):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        assert by_ref(got)["feat/abc-7-read-lines"].strength == "correlation"
        text = rp.render(got)
        assert "correlation only: a shared ticket or name, no conflict shown" in text
        assert "Strength: **correlation**" in text

    def test_removing_what_another_repository_uses_is_a_conflict(self):
        m = rp.Merged("orders", "x", (), SINCE, SINCE, None, "a@x", "a" * 40, "b" * 40)
        c = rp.Case(m, brief={"recommendation": "review_with", "index": {},
                              "contract_breaks": [{"name": "order_line"}]})
        assert c.strength == "conflict"
        c = rp.Case(m, brief={"recommendation": "review_with", "index": {}, "contracts": [
            {"parties": [{"repo": "billing", "uses": "consumes"}]}]})
        assert c.strength == "dependency"


class TestNothingFoundIsNotTheSameAsCouldNotTell:
    def test_with_one_repository_a_silence_is_not_clear(self, tmp_path):
        root = tmp_path / "src"
        orders = repo(root, "orders", {"app/models.py": "__tablename__ = 'order_line'\n"})
        merge(orders, "feat/abc-9-readme", {"README.md": "hello\n"}, pr=4)
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: []\n")
        got = rp.run(Config.load(str(p)), LocalGitAdapter(str(root)), since=SINCE, author=None)
        assert [c.outcome for c in got.cases] == ["coverage"]
        assert got.clear == [] and len(got.uncovered) == 1
        text = rp.render(got)
        assert "## Changes Ledger could not see enough to judge" in text
        assert "only one repository was visible" in text
        assert "## Changes where nothing was found" not in text

    def test_insufficient_data_is_never_counted_as_something_to_say(self):
        m = rp.Merged("orders", "x", (), SINCE, SINCE, None, "a@x", "a" * 40, "b" * 40)
        c = rp.Case(m, brief={"recommendation": "insufficient_data", "index": {}})
        assert c.outcome == "coverage"
        c = rp.Case(m, brief={"recommendation": "proceed",
                              "index": {"unreadable_repos": ["billing"]}})
        assert c.outcome == "coverage"
        c = rp.Case(m, brief={"recommendation": "proceed", "index": {}})
        assert c.outcome == "clear"


def at(days_ago):
    d = (date.today() - timedelta(days=days_ago)).isoformat() + "T12:00:00"
    return {"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d}


def dgit(path, *args, days_ago, email="ann@example.com"):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**ENV, **at(days_ago), "GIT_AUTHOR_EMAIL": email, "HOME": str(path)})


def dated_repo(root, name, files, *, days_ago):
    p = root / name
    p.mkdir(parents=True)
    dgit(p, "init", "-q", "-b", "main", days_ago=days_ago)
    dgit(p, "remote", "add", "origin", f"git@github.com:acme/{name}.git", days_ago=days_ago)
    for f, text in files.items():
        (p / f).parent.mkdir(parents=True, exist_ok=True)
        (p / f).write_text(text)
    dgit(p, "add", "-A", days_ago=days_ago)
    dgit(p, "commit", "-q", "-m", "start", days_ago=days_ago)
    return p


def commit(p, files, msg, *, days_ago):
    for f, text in files.items():
        (p / f).parent.mkdir(parents=True, exist_ok=True)
        (p / f).write_text(text)
    dgit(p, "add", "-A", days_ago=days_ago)
    dgit(p, "commit", "-q", "-m", msg, days_ago=days_ago)


class TestNoHindsight:
    def _estate(self, tmp_path, *, billing_adds_it_days_ago):
        root = tmp_path / "src"
        orders = dated_repo(root, "orders", {"app/models.py": "x = 1\n"}, days_ago=30)
        billing = dated_repo(root, "billing", {"app/db.py": "y = 1\n"}, days_ago=30)
        # The change: started 10 days ago, merged 9 days ago, touches `order_line`.
        dgit(orders, "checkout", "-q", "-b", "tidy-order-lines", days_ago=10)
        commit(orders, {"app/archive.py": "class A:\n    __tablename__ = 'order_line'\n"},
               "archive", days_ago=10)
        dgit(orders, "checkout", "-q", "main", days_ago=9)
        dgit(orders, "merge", "-q", "--no-ff", "tidy-order-lines", "-m",
             "Merge pull request #3 from acme/tidy-order-lines", days_ago=9)
        # Billing starts using the same table: before, or after, that change started.
        commit(billing, {"app/db.py": "__tablename__ = 'order_line'\n"}, "use order_line",
               days_ago=billing_adds_it_days_ago)
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: []\n")
        return Config.load(str(p)), root

    def test_a_contract_another_repository_took_up_later_is_not_used(self, tmp_path):
        cfg, root = self._estate(tmp_path, billing_adds_it_days_ago=5)
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=date.today() - timedelta(days=20),
                     author=None)
        case = by_ref(got)["tidy-order-lines"]
        assert case.brief["recommendation"] == "proceed" and case.outcome == "clear"
        assert case.as_of == date.today() - timedelta(days=10)
        assert case.checked == ["billing"]

    def test_one_it_already_had_that_day_is(self, tmp_path):
        cfg, root = self._estate(tmp_path, billing_adds_it_days_ago=12)
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=date.today() - timedelta(days=20),
                     author=None)
        assert by_ref(got)["tidy-order-lines"].brief["recommendation"] == "review_with"

    def test_a_repository_that_did_not_exist_yet_is_not_read(self, tmp_path):
        root = tmp_path / "src"
        orders = dated_repo(root, "orders", {"app/models.py": "x = 1\n"}, days_ago=30)
        dated_repo(root, "billing", {"app/db.py": "__tablename__ = 'order_line'\n"}, days_ago=3)
        dgit(orders, "checkout", "-q", "-b", "tidy-order-lines", days_ago=10)
        commit(orders, {"app/archive.py": "__tablename__ = 'order_line'\n"}, "a", days_ago=10)
        dgit(orders, "checkout", "-q", "main", days_ago=9)
        dgit(orders, "merge", "-q", "--no-ff", "tidy-order-lines", "-m",
             "Merge pull request #3 from acme/tidy-order-lines", days_ago=9)
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: []\n")
        got = rp.run(Config.load(str(p)), LocalGitAdapter(str(root)),
                     since=date.today() - timedelta(days=20), author=None)
        case = by_ref(got)["tidy-order-lines"]
        assert case.not_yet == ["billing"] and case.brief["recommendation"] == "proceed"


class TestSquashMerges:
    def test_a_squash_merged_pull_request_is_replayed(self, tmp_path):
        root = tmp_path / "src"
        orders = repo(root, "orders", {"app/models.py": "x = 1\n"})
        billing = repo(root, "billing", {"app/db.py": "y = 1\n"})
        merge(billing, "feat/abc-7-split-lines", {"app/split.py": "y = 2\n"}, pr=2,
              email="bob@example.com")
        (orders / "app" / "read.py").write_text("z = 1\n")
        git(orders, "add", "-A")
        git(orders, "commit", "-q", "-m", "ABC-7 read order lines (#12)")
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: []\n")
        got = rp.run(Config.load(str(p)), LocalGitAdapter(str(root)), since=SINCE, author=None)
        case = by_ref(got)["pr-12"]
        assert case.change.kind == "squash" and case.change.pr == 12
        assert case.change.tickets == ("ABC-7",)
        assert case.brief["subject"]["own_commits"] == 1
        assert case.brief["recommendation"] == "review_with"
        assert "ABC-7 read order lines (#12)" in rp.render(got)

    def test_a_commit_with_no_pull_request_is_counted_not_replayed(self, tmp_path):
        root = tmp_path / "src"
        orders = repo(root, "orders", {"a.py": "x\n"})
        repo(root, "billing", {"b.py": "x\n"})
        (orders / "c.py").write_text("y\n")
        git(orders, "add", "-A")
        git(orders, "commit", "-q", "-m", "a direct push")
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        got = rp.run(Config.load(str(p)), LocalGitAdapter(str(root)), since=SINCE, author=None)
        assert got.cases == []
        assert any("rebase-merged or pushed directly" in g for g in got.gaps())
        assert "without a pull request in them" in rp.render(got)


class TestTheCommand:
    def test_it_writes_the_report(self, estate, tmp_path):
        cfg, root = estate
        out = tmp_path / "report.md"
        res = CliRunner().invoke(app, ["replay", "--config", str(tmp_path / "ledger.yml"),
                                       "--local-root", str(root), "--everyone", "--days", "7",
                                       "--out", str(out)])
        assert res.exit_code == 0, res.output
        assert "replayed 4 change(s): 3 with something to say" in res.output
        assert out.read_text().startswith("# Ledger replay")
        import json
        side = json.loads(out.with_suffix(".json").read_text())
        assert len(side["cases"]) == 4 and {"id", "repo", "ref", "sha", "outcome"} <= set(side["cases"][0])
