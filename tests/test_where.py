"""Each leading item of a brief says where to read it on this machine.

Real use, three weeks of reviews: six of twenty sessions stopped at "that
repository is not checked out here, so I couldn't verify it", while every one
of those repositories was in the ledger's own mirrors.
"""

from __future__ import annotations

import subprocess

from ledger import where
from ledger.surfaces import hook as h


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True,
                          text=True).stdout.strip()


def estate(tmp_path):
    """A mirror root holding `mailer.git` (default `main`, branch `feat/x`, a
    decision file) and an empty checkout root."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@t"); git(work, "config", "user.name", "t")
    (work / "docs").mkdir()
    (work / "docs" / "adr.md").write_text("# decided\n")
    git(work, "add", "."); git(work, "commit", "-qm", "init")
    git(work, "checkout", "-qb", "feat/x")
    (work / "app.py").write_text("x = 1\n")
    git(work, "add", "."); git(work, "commit", "-qm", "x")
    sha = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "main")
    mirrors, clones = tmp_path / "mirrors", tmp_path / "clones"
    clones.mkdir()
    mirrors.mkdir()
    subprocess.run(["git", "clone", "-q", "--mirror", str(work), str(mirrors / "mailer.git")],
                   check=True, capture_output=True)
    return [clones, mirrors], mirrors / "mailer.git", sha


def run(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True).stdout


class TestFetchLine:
    def test_a_commit_in_the_mirror_is_one_command_that_shows_it(self, tmp_path):
        roots, mirror, sha = estate(tmp_path)
        line = where.fetch_line("impact", {"repo": "Acme/mailer", "ref": "stage", "sha": sha}, roots)
        assert line == f"git -C {mirror} show {sha}"
        assert "x = 1" in run(line), "the command runs as given"

    def test_a_branch_is_its_diff_against_the_default(self, tmp_path):
        roots, mirror, _ = estate(tmp_path)
        line = where.fetch_line("impact", {"repo": "mailer", "ref": "feat/x"}, roots)
        assert line == f"git -C {mirror} diff main...feat/x"
        assert "+x = 1" in run(line)

    def test_an_open_branch_is_its_whole_diff_not_its_tip(self, tmp_path):
        """An unmerged branch carries its tip sha too; `show` on it would hide
        every earlier commit on the branch."""
        roots, mirror, sha = estate(tmp_path)
        line = where.fetch_line("impact", {"repo": "mailer", "ref": "feat/x", "sha": sha}, roots)
        assert line == f"git -C {mirror} diff main...feat/x"

    def test_a_checkout_is_read_through_origin_and_wins(self, tmp_path):
        roots, mirror, _ = estate(tmp_path)
        clone = roots[0] / "mailer"
        subprocess.run(["git", "clone", "-q", str(mirror), str(clone)], check=True, capture_output=True)
        line = where.fetch_line("impact", {"repo": "mailer", "ref": "feat/x"}, roots)
        assert line == f"git -C {clone} diff origin/main...origin/feat/x"
        assert "+x = 1" in run(line)

    def test_a_decision_is_the_file_itself(self, tmp_path):
        roots, mirror, _ = estate(tmp_path)
        line = where.fetch_line("decision", {"repo": "mailer", "path": "docs/adr.md"}, roots)
        assert line == f"git -C {mirror} show HEAD:docs/adr.md"
        assert "# decided" in run(line)

    def test_a_commit_not_fetched_yet_says_how_to_fetch(self, tmp_path):
        roots, mirror, _ = estate(tmp_path)
        line = where.fetch_line("impact", {"repo": "mailer", "ref": "stage", "sha": "0" * 12}, roots)
        assert line.startswith(f"{'0' * 12} is not in {mirror} yet: git -C {mirror} fetch")

    def test_a_repository_nowhere_here_says_so(self, tmp_path):
        roots, _, _ = estate(tmp_path)
        line = where.fetch_line("impact", {"repo": "vox", "ref": "feat/y"}, roots)
        assert line.startswith("vox is not on this machine")

    def test_no_roots_no_line(self):
        assert where.fetch_line("impact", {"repo": "mailer", "ref": "x"}, []) is None


class TestInTheBrief:
    def test_the_lead_carries_the_command_and_the_hook_shows_it(self, tmp_path):
        roots, mirror, sha = estate(tmp_path)
        b = {"schema": 4, "subject": {"repo": "core", "ref": "sk-1-x"},
             "recommendation": "review_with", "confidence": "medium", "reasons": [],
             "actors": [], "contracts": [], "decisions": [],
             "impacts": [{"repo": "mailer", "ref": "stage", "sha": sha, "where": "stage",
                          "author": "kt"}],
             "ranked": {"lead": [{"key": f"impact:mailer:{sha}", "kind": "impact",
                                  "check": "how to check: read mailer"}],
                        "also": {"count": 0, "by_kind": {}, "repos": {}, "keys": []},
                        "repeated": {}, "nothing": False}}
        where.annotate(b, roots)
        assert b["ranked"]["lead"][0]["fetch"] == f"git -C {mirror} show {sha}"
        assert f"get it: git -C {mirror} show {sha}" in h.summarise(b)

    def test_without_roots_the_brief_is_unchanged(self):
        b = {"ranked": {"lead": [{"key": "impact:mailer:abc"}]},
             "impacts": [{"repo": "mailer", "ref": "x", "sha": "abc"}]}
        assert "fetch" not in where.annotate(b, [])["ranked"]["lead"][0]
