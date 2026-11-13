"""A mirror-only server still knows which pull requests are open: the list
comes from the forge, the code from disk. Real: 140 of 242 open pull requests
were not in flight, and a review waiting since September was not found."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from ledger.adapters.vcs import localgit
from ledger.adapters.vcs.base import ForgeUnavailable, PullRef
from ledger.adapters.vcs.localgit import LocalGitAdapter, forge_name
from ledger.pipeline.collect import collect

OLD = "2026-09-16T10:00:00+00:00"


def git(repo, *a, when=OLD):
    env = {"GIT_AUTHOR_NAME": "ann", "GIT_AUTHOR_EMAIL": "ann@example.com", "GIT_COMMITTER_NAME": "ann",
           "GIT_COMMITTER_EMAIL": "ann@example.com", "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "HOME": str(repo.parent), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=env)


class Forge:
    source = "github"
    host = "github.com"

    def __init__(self, org="Acme", fail=False):
        self.org, self.fail, self.calls = org, fail, 0

    def list_open_pulls(self, repo):
        self.calls += 1
        if self.fail:
            raise ForgeUnavailable("github", "rate limited")
        return [PullRef(repo=repo, number=8462, title="SKY-8638 Settle claims", head_ref="feat/settle",
                        base_ref="main", head_sha="a" * 40, author="ann",
                        url=f"https://github.com/{repo}/pull/8462")]


@pytest.fixture(autouse=True)
def _fresh_cache():
    localgit._PULLS.clear()
    yield
    localgit._PULLS.clear()


@pytest.fixture
def mirrors(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("x = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "start")
    git(work, "checkout", "-qb", "feat/settle")
    (work / "a.py").write_text("x = 2\n")
    git(work, "commit", "-qam", "settle")
    git(work, "checkout", "-q", "main")
    root = tmp_path / "mirrors"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--mirror", str(work), str(root / "core.git")], check=True)
    git(root / "core.git", "remote", "set-url", "origin", "https://github.com/Acme/core.git")
    return root


def test_the_origin_names_the_forge_repository(mirrors, tmp_path):
    assert forge_name(mirrors / "core.git") == ("github.com", "Acme/core")
    git(mirrors / "core.git", "remote", "set-url", "origin", "git@bitbucket.org:acme-ws/core.git")
    assert forge_name(mirrors / "core.git") == ("bitbucket.org", "acme-ws/core")


def test_an_old_branch_with_an_open_pull_request_is_in_flight(mirrors):
    got = collect([LocalGitAdapter(mirrors, pulls_from=[Forge()])], ("SKY",),
                  now=datetime(2026, 11, 11, tzinfo=timezone.utc))
    prs = [c for c in got.changes if c.kind == "pr"]
    assert [(c.repo, c.ref, c.pr_number, c.tickets) for c in prs] == [("core", "feat/settle", 8462, ("SKY-8638",))]
    # Who pushed the head and when, as for a branch; and where it merges.
    assert prs[0].author == "ann@example.com" and prs[0].base_ref == "main"
    assert prs[0].updated_at == datetime.fromisoformat(OLD)


def test_without_a_forge_it_is_branches_only_as_before(mirrors):
    got = collect([LocalGitAdapter(mirrors)], ("SKY",), now=datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert [c for c in got.changes if c.kind == "pr"] == []


def test_another_organisations_forge_is_not_asked(mirrors):
    forge = Forge(org="Other")
    assert LocalGitAdapter(mirrors, pulls_from=[forge]).list_open_pulls("core") == []
    assert forge.calls == 0


def test_the_answer_is_reused_and_survives_a_failing_forge(mirrors):
    forge = Forge()
    assert len(LocalGitAdapter(mirrors, pulls_from=[forge]).list_open_pulls("core")) == 1
    assert len(LocalGitAdapter(mirrors, pulls_from=[forge]).list_open_pulls("core")) == 1
    assert forge.calls == 1
    localgit._PULLS[("github", "Acme/core")] = (0.0, localgit._PULLS[("github", "Acme/core")][1])
    forge.fail = True
    assert len(LocalGitAdapter(mirrors, pulls_from=[forge]).list_open_pulls("core")) == 1


def test_a_failing_forge_with_nothing_cached_degrades_the_repository(mirrors):
    got = collect([LocalGitAdapter(mirrors, pulls_from=[Forge(fail=True)])], ("SKY",),
                  now=datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert got.degraded_repos == ("core",)

