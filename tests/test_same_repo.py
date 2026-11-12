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
