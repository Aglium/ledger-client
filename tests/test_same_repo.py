"""Open work in the reviewer's own repository that changes the same files.

Real: on one week every `missed` verdict was an open pull request in the same
repository changing the same file, under the same ticket, while the brief said
`proceed`. A shared file makes an item; a shared ticket alone is context.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from ledger import service
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.config import Config
from ledger.core.join import Change, JoinResult, Sibling, collapse_integration

NOW = datetime.now(timezone.utc)


def git(repo, *a, when=None):
    when = (when or NOW - timedelta(hours=2)).isoformat()
    env = {"GIT_AUTHOR_NAME": "ann", "GIT_AUTHOR_EMAIL": "ann@example.com", "GIT_COMMITTER_NAME": "ann",
           "GIT_COMMITTER_EMAIL": "ann@example.com", "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "HOME": str(repo.parent), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=env)


def branch(repo, name, files):
    git(repo, "checkout", "-qb", name, "main")
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


@pytest.fixture
def estate(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app").mkdir()
    (repo / "app" / "docs.py").write_text("def build():\n    return 1\n")
    (repo / "app" / "other.py").write_text("x = 1\n")
    (repo / "CHANGELOG.md").write_text("# log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start", when=NOW - timedelta(days=3))
    branch(repo, "feat/SKY-1-mine", {"app/docs.py": "def build():\n    return 2\n",
                                     "CHANGELOG.md": "# log\n- mine\n"})
    branch(repo, "feat/SKY-9-theirs", {"app/docs.py": "def build():\n    return 3\n"})
    branch(repo, "feat/SKY-1-elsewhere", {"app/other.py": "x = 2\n"})
    branch(repo, "feat/SKY-7-log", {"CHANGELOG.md": "# log\n- log\n"})
    root = tmp_path / "mirrors"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--mirror", str(repo), str(root / "core.git")], check=True)
    cfg = tmp_path / "ledger.yml"
    cfg.write_text(f"org: acme\nproject_keys: [SKY]\nmirror_root: {root}\n")
    return Config.load(cfg), LocalGitAdapter(root)


def brief(estate, ref="feat/SKY-1-mine"):
    cfg, vcs = estate
    return service.compute_brief(cfg, [vcs], "core", ref,
                                 lambda repos: service.LiveIndex(vcs, repos, None))


def test_an_open_change_to_the_same_file_is_an_item(estate):
    b = brief(estate)
    got = {i.ref: i for i in b.impacts if i.repo == "core"}
    assert "feat/SKY-9-theirs" in got
    assert "`docs.py`" in got["feat/SKY-9-theirs"].how
    assert b.recommendation not in ("proceed", "insufficient_data")
    assert any("open change(s) in this repository" in r for r in b.reasons)
    assert any("feat/SKY-9-theirs" in x["label"] for x in b.ranked["lead"])


def test_a_shared_changelog_is_not_overlap(estate):
    assert "feat/SKY-7-log" not in {i.ref for i in brief(estate).impacts}


def test_the_same_ticket_alone_is_a_note_not_an_item(estate):
    b = brief(estate)
    assert "feat/SKY-1-elsewhere" not in {i.ref for i in b.impacts if i.repo == "core"}
    assert any(r.startswith("note: open in this repository under the same ticket")
               and "feat/SKY-1-elsewhere" in r for r in b.reasons)


def _merge(ref, hours, tickets=("SKY-5",), where="stage"):
    return Sibling(Change(repo="core", ref=ref, tickets=tickets, kind="integration",
                          integration_ref=where, updated_at=NOW - timedelta(hours=hours)),
                   shared_tickets=tickets)


class TestMergesUnderOneTicket:
    def test_they_collapse_to_the_most_recent(self):
        subject = Change(repo="web", ref="feat/SKY-5-x", tickets=("SKY-5",))
        got, counts = collapse_integration(JoinResult(subject=subject, siblings=[
            _merge("feat/SKY-5-a", 5), _merge("feat/SKY-5-b", 1), _merge("feat/SKY-5-c", 9),
            _merge("feat/SKY-5-d", 2, where="release/1.0")]))
        assert sorted(s.change.ref for s in got.siblings) == ["feat/SKY-5-b", "feat/SKY-5-d"]
        assert counts == {("core", "feat/SKY-5-b"): (3, "stage")}

    def test_branches_and_pull_requests_are_left_alone(self):
        subject = Change(repo="web", ref="feat/SKY-5-x", tickets=("SKY-5",))
        open_ = [Sibling(Change(repo="core", ref=f"feat/SKY-5-{n}", tickets=("SKY-5",)),
                         shared_tickets=("SKY-5",)) for n in "ab"]
        got, counts = collapse_integration(JoinResult(subject=subject, siblings=open_))
        assert len(got.siblings) == 2 and counts == {}


def test_more_than_are_listed_are_named_and_counted_right(estate, tmp_path, monkeypatch):
    cfg, vcs = estate
    monkeypatch.setattr(service, "SAME_REPO_SHOWN", 1)
    work = tmp_path / "work"
    branch(work, "feat/SKY-8-also", {"app/docs.py": "def build():\n    return 4\n"})
    subprocess.run(["git", "-C", str(tmp_path / "mirrors" / "core.git"), "fetch", "-q", str(work),
                    "+refs/heads/*:refs/heads/*"], check=True)
    b = brief(estate)
    items = [i.ref for i in b.impacts if i.repo == "core"]
    assert len(items) == 1
    line = next(r for r in b.reasons if "open change(s) in this repository" in r)
    assert line.startswith("1 open change(s)")
    other = ({"feat/SKY-9-theirs", "feat/SKY-8-also"} - set(items)).pop()
    assert f"also `{other}`" in line


def test_the_cap_counts_only_what_was_compared(estate, monkeypatch):
    monkeypatch.setattr(service, "SAME_REPO_CAP", 1)
    real = service.LiveIndex.open_in_repo

    def subject_first(self, repo):
        got = real(self, repo)
        order = ["feat/SKY-1-mine", "release/9", "feat/SKY-9-theirs"]
        return sorted(got, key=lambda c: order.index(c.ref) if c.ref in order else 9)
    monkeypatch.setattr(service.LiveIndex, "open_in_repo", subject_first)
    # The subject is first and must not use the one place.
    assert "feat/SKY-9-theirs" in {i.ref for i in brief(estate).impacts}


def test_a_merged_item_text_is_not_given_to_an_open_branch_of_that_name():
    from ledger.core.rank import MERGES, items_of
    many = {"repo": "core", "ref": "feat/SKY-5-a", "kind": "integration", "where": "stage",
            "sha": "abc", "shared_tickets": ["SKY-5"],
            "how": f"{MERGES}100 merges into `stage` under SKY-5; the others are the same work"}
    one = {"repo": "web", "ref": "feat/SKY-5-b", "kind": "branch", "shared_tickets": ["SKY-5"]}
    got = {i.label: i for i in items_of({"impacts": [many, one], "ref": "feat/SKY-5-x"})}
    assert not got["web branch `feat/SKY-5-b`"].sole


def test_a_grep_that_times_out_loses_only_the_mounts():
    from ledger.adapters.vcs.fake import FakeAdapter
    from ledger.pipeline.scan import mount_paths

    class Slow(FakeAdapter):
        def grep_paths(self, repo, ref, names):
            raise subprocess.TimeoutExpired("git grep", 60)
    assert mount_paths(Slow(), "core", "main") == []


def test_a_port_is_read_against_what_it_is_cut_from(estate, tmp_path):
    """A branch cut from `stage` read against `main` carries all of `stage`."""
    work = tmp_path / "work"
    git(work, "checkout", "-qb", "stage", "main")
    (work / "app" / "docs.py").write_text("def build():\n    return 5\n")
    git(work, "commit", "-qam", "on stage")
    git(work, "checkout", "-qb", "feat/SKY-6-port", "stage")
    (work / "app" / "port.py").write_text("y = 1\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "port")
    git(work, "checkout", "-q", "main")
    subprocess.run(["git", "-C", str(tmp_path / "mirrors" / "core.git"), "fetch", "-q", str(work),
                    "+refs/heads/*:refs/heads/*"], check=True)
    refs = {i.ref for i in brief(estate).impacts if i.repo == "core"}
    assert "feat/SKY-6-port" not in refs and "feat/SKY-9-theirs" in refs
