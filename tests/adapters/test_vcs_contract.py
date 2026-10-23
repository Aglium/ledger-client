"""ONE contract suite, run against EVERY VcsAdapter implementation.

This is the file that keeps the port honest. Three adapters written against a
prose description drift; three adapters that must pass the same assertions do
not. Adding a forge means adding a harness here, not a new test file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from ledger.adapters.vcs.base import BranchRef, ForgeUnavailable, PullRef, RepoRef
from ledger.adapters.vcs.bitbucket import BitbucketAdapter
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.adapters.vcs.github import GitHubAdapter
from ledger.adapters.vcs.localgit import LocalGitAdapter

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
REPO_GH = "example-org/intake-service"
REPO_BB = "example-ws/legacy-core"


class Harness:
    """Per-forge setup for the shared assertions below."""

    def __init__(self, adapter, repo, *, make_unavailable, comment_store):
        self.adapter = adapter
        self.repo = repo
        self.make_unavailable = make_unavailable
        self.comment_store = comment_store


# --------------------------------------------------------------------------- fake
@pytest.fixture
def fake_harness():
    a = FakeAdapter(
        repos=[RepoRef(full_name=REPO_GH, default_branch="main", host="github.com")],
        branches={REPO_GH: [BranchRef(repo=REPO_GH, name="feat/abc-321-x", head_sha="a" * 40, updated_at=NOW)]},
        pulls={
            REPO_GH: [
                PullRef(repo=REPO_GH, number=7, title="ABC-321 bind documents", head_ref="feat/abc-321-x",
                        base_ref="main", head_sha="a" * 40, author="alice", updated_at=NOW),
                PullRef(repo=REPO_GH, number=8, title="closed one", head_ref="old", base_ref="main",
                        head_sha="b" * 40, state="closed"),
            ]
        },
    )

    def unavailable():
        a._unavailable = True

    a._ahead = {(REPO_GH, "main", "feat/abc-321-x"): 2}
    a._tree = {REPO_GH: {"README.md": "# readme"}}
    h = Harness(a, REPO_GH, make_unavailable=unavailable, comment_store=lambda: list(a.comments.values()))
    h.ahead_case = ("main", "feat/abc-321-x", 2)
    h.tree_case = ("main", "README.md", "# readme")
    h.check_store = lambda: [(v[0], v[1]) for v in a.checks.values()]
    return h


# ------------------------------------------------------------------------- github
@pytest.fixture
def github_harness():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/orgs/example-org/repos").respond(
            200, json=[{"full_name": REPO_GH, "default_branch": "main", "html_url": "u", "archived": False},
                       {"full_name": "example-org/dead", "default_branch": "main", "archived": True}]
        )
        mock.get(f"https://api.github.com/repos/{REPO_GH}/branches").respond(
            200, json=[{"name": "feat/abc-321-x", "commit": {"sha": "a" * 40}}]
        )
        mock.get(f"https://api.github.com/repos/{REPO_GH}/pulls").respond(
            200, json=[{"number": 7, "title": "ABC-321 bind documents",
                        "head": {"ref": "feat/abc-321-x", "sha": "a" * 40}, "base": {"ref": "main"},
                        "user": {"login": "alice"}, "html_url": "u", "updated_at": "2026-09-15T00:00:00Z"}]
        )
        store: list[str] = []
        comments: list[dict] = []
        mock.get(f"https://api.github.com/repos/{REPO_GH}/issues/7/comments").mock(
            side_effect=lambda req: httpx.Response(200, json=list(comments))
        )

        def _create(req):
            import json as _j
            body = _j.loads(req.content)["body"]
            comments.append({"id": 100, "body": body})
            store.append(body)
            return httpx.Response(201, json={"id": 100})

        def _update(req):
            import json as _j
            body = _j.loads(req.content)["body"]
            comments[0]["body"] = body
            store.append(body)
            return httpx.Response(200, json={"id": 100})

        mock.post(f"https://api.github.com/repos/{REPO_GH}/issues/7/comments").mock(side_effect=_create)
        mock.patch(f"https://api.github.com/repos/{REPO_GH}/issues/comments/100").mock(side_effect=_update)

        checks: list[dict] = []
        mock.get(f"https://api.github.com/repos/{REPO_GH}/commits/{'a' * 40}/check-runs").mock(
            side_effect=lambda req: httpx.Response(200, json={"check_runs": list(checks)})
        )

        def _create_check(req):
            import json as _j
            checks.append({"id": 500, **_j.loads(req.content)})
            return httpx.Response(201, json={"id": 500})

        def _update_check(req):
            import json as _j
            checks[0].update(_j.loads(req.content))
            return httpx.Response(200, json={"id": 500})

        mock.post(f"https://api.github.com/repos/{REPO_GH}/check-runs").mock(side_effect=_create_check)
        mock.patch(f"https://api.github.com/repos/{REPO_GH}/check-runs/500").mock(side_effect=_update_check)

        mock.get(f"https://api.github.com/repos/{REPO_GH}/compare/main...feat/abc-321-x").respond(
            200, json={"ahead_by": 2, "files": []})
        mock.get(f"https://api.github.com/repos/{REPO_GH}/compare/main...no/such/branch-xyz").respond(404)
        import base64 as _b64
        mock.get(f"https://api.github.com/repos/{REPO_GH}/contents/README.md").respond(
            200, json={"encoding": "base64", "content": _b64.b64encode(b"# readme").decode()})
        mock.get(f"https://api.github.com/repos/{REPO_GH}/contents/no-such-file.md").respond(404)

        adapter = GitHubAdapter("example-org", "tok")

        def unavailable():
            mock.get(f"https://api.github.com/repos/{REPO_GH}/pulls").respond(500)
            mock.get("https://api.github.com/orgs/example-org/repos").respond(500)

        h = Harness(adapter, REPO_GH, make_unavailable=unavailable,
                    comment_store=lambda: [c["body"] for c in comments])
        h.check_store = lambda: [(c.get("conclusion"), c.get("output", {}).get("title")) for c in checks]
        h.ahead_case = ("main", "feat/abc-321-x", 2)
        h.tree_case = ("main", "README.md", "# readme")
        yield h


# ---------------------------------------------------------------------- bitbucket
@pytest.fixture
def bitbucket_harness():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.bitbucket.org/2.0/repositories/example-ws").respond(
            200, json={"values": [{"full_name": REPO_BB, "mainbranch": {"name": "master"},
                                   "links": {"html": {"href": "u"}}}], "next": None}
        )
        mock.get(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/refs/branches").respond(
            200, json={"values": [{"name": "feat/abc-321-x",
                                   "target": {"hash": "a" * 40, "date": "2026-09-15T00:00:00Z"}}], "next": None}
        )
        mock.get(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/pullrequests").respond(
            200, json={"values": [{"id": 7, "title": "ABC-321 bind documents",
                                   "source": {"branch": {"name": "feat/abc-321-x"}, "commit": {"hash": "a" * 40}},
                                   "destination": {"branch": {"name": "master"}},
                                   "author": {"nickname": "alice"}, "links": {"html": {"href": "u"}},
                                   "updated_on": "2026-09-15T00:00:00Z"}], "next": None}
        )
        comments: list[dict] = []
        mock.get(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/pullrequests/7/comments").mock(
            side_effect=lambda req: httpx.Response(200, json={"values": list(comments), "next": None})
        )

        def _create(req):
            import json as _j
            raw = _j.loads(req.content)["content"]["raw"]
            comments.append({"id": 100, "content": {"raw": raw}})
            return httpx.Response(201, json={"id": 100})

        def _update(req):
            import json as _j
            comments[0]["content"]["raw"] = _j.loads(req.content)["content"]["raw"]
            return httpx.Response(200, json={"id": 100})

        mock.post(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/pullrequests/7/comments").mock(side_effect=_create)
        mock.put(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/pullrequests/7/comments/100").mock(side_effect=_update)

        mock.get(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/commits/feat/abc-321-x").respond(
            200, json={"values": [{"hash": "a" * 40}, {"hash": "b" * 40}], "next": None})
        mock.get(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/commits/no/such/branch-xyz").respond(404)

        adapter = BitbucketAdapter("example-ws", "u", "p")

        def unavailable():
            mock.get(f"https://api.bitbucket.org/2.0/repositories/{REPO_BB}/pullrequests").respond(429)
            mock.get("https://api.bitbucket.org/2.0/repositories/example-ws").respond(429)

        h = Harness(adapter, REPO_BB, make_unavailable=unavailable,
                    comment_store=lambda: [c["content"]["raw"] for c in comments])
        h.check_store = lambda: []
        h.ahead_case = ("master", "feat/abc-321-x", 2)
        yield h


@pytest.fixture
def localgit_harness(tmp_path):
    """A real git repo on disk. Proves the port is not GitHub-shaped by accident."""
    import subprocess

    root = tmp_path / "root"
    repo = root / "intake-service"
    (repo).mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True)
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    run("add", "."); run("commit", "-qm", "ABC-321 init")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("branch", "-q", "work")
    run("checkout", "-q", "work")
    run("commit", "-q", "--allow-empty", "-m", "ABC-321 the branch's own work")
    run("update-ref", "refs/remotes/origin/feat/abc-321-x", "HEAD")
    run("reset", "-q", "--hard", "HEAD~1")
    run("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    adapter = LocalGitAdapter(root)
    missing = root / "gone"

    def unavailable():
        import shutil
        shutil.rmtree(repo)

    h = Harness(adapter, "intake-service", make_unavailable=unavailable, comment_store=lambda: [])
    h.check_store = lambda: []
    h.ahead_case = ("main", "feat/abc-321-x", 1)
    h.tree_case = ("main", "f.txt", "x")
    h.pickaxe_case = ("main", "x", "f.txt")
    return h


@pytest.fixture
def mirror_harness(tmp_path, localgit_harness):
    """A bare mirror of the same repository -- what a server holds.

    No working tree, branches under refs/heads rather than refs/remotes/origin.
    It is a harness rather than a special case because the point is that the
    port does not care: every assertion below has to pass against it too.
    """
    import pathlib
    import subprocess

    src = pathlib.Path(localgit_harness.adapter.root) / localgit_harness.repo
    root = tmp_path / "mirrors"
    root.mkdir()
    mirror = root / "intake-service.git"
    subprocess.run(["git", "clone", "--mirror", "-q", str(src), str(mirror)],
                   check=True, capture_output=True)
    # A mirror of a clone copies its remote refs; a mirror of a server-side
    # repository has its branches under refs/heads. Make it look like the
    # latter, which is what `git clone --mirror <url>` produces.
    subprocess.run(["git", "-C", str(mirror), "fetch", "origin",
                    "+refs/remotes/origin/*:refs/heads/*"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(mirror), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True, capture_output=True)

    adapter = LocalGitAdapter(root)

    def unavailable():
        import shutil
        shutil.rmtree(mirror)

    h = Harness(adapter, "intake-service", make_unavailable=unavailable, comment_store=lambda: [])
    h.check_store = lambda: []
    h.ahead_case = ("main", "feat/abc-321-x", 1)
    h.tree_case = ("main", "f.txt", "x")
    return h


@pytest.fixture(params=["fake_harness", "github_harness", "bitbucket_harness", "localgit_harness",
                        "mirror_harness"])
def h(request):
    """Every assertion below runs once per adapter, mirrors included."""
    return request.getfixturevalue(request.param)


# ------------------------------------------------------------------ the contract
class TestVcsAdapterContract:
    def test_source_is_a_stable_identifier(self, h):
        assert isinstance(h.adapter.source, str) and h.adapter.source

    def test_list_repos_returns_repo_refs_with_a_default_branch(self, h):
        repos = h.adapter.list_repos()
        assert repos and all(isinstance(r, RepoRef) for r in repos)
        assert all(r.full_name and r.default_branch and r.host for r in repos)

    def test_list_repos_excludes_archived(self, h):
        assert not any("dead" in r.full_name for r in h.adapter.list_repos())

    def test_declares_its_capabilities(self, h):
        assert isinstance(h.adapter.supports_comments, bool)
        assert isinstance(h.adapter.supports_checks, bool)

    def test_list_branches_returns_branch_refs(self, h):
        branches = h.adapter.list_branches(h.repo)
        assert branches and all(isinstance(b, BranchRef) for b in branches)
        assert all(b.repo == h.repo and b.name and b.head_sha for b in branches)

    def test_list_open_pulls_returns_only_open(self, h):
        pulls = h.adapter.list_open_pulls(h.repo)
        if not h.adapter.supports_comments:
            # A source with no PR concept truthfully returns none. That is a
            # result, not a degradation -- its branches still feed the join.
            assert pulls == []
            return
        assert pulls and all(isinstance(p, PullRef) for p in pulls)
        assert all(p.state == "open" for p in pulls)
        assert all(p.head_ref and p.base_ref for p in pulls)

    def test_upsert_comment_updates_in_place_never_appends(self, h):
        marker = "ledger:siblings"
        if not h.adapter.supports_comments:
            with pytest.raises(ForgeUnavailable):
                h.adapter.upsert_comment(h.repo, 7, marker, "body")
            return
        first = h.adapter.upsert_comment(h.repo, 7, marker, "first body")
        second = h.adapter.upsert_comment(h.repo, 7, marker, "second body")
        assert first == second, "a second upsert must reuse the same comment"
        bodies = h.comment_store()
        assert sum(1 for b in bodies if marker in b) == 1, "exactly one marked comment must exist"
        assert any("second body" in b for b in bodies)

    def test_unavailable_forge_raises_never_returns_empty(self, h):
        """T20: a short list from a degraded forge is a false negative, not a result."""
        h.make_unavailable()
        with pytest.raises(ForgeUnavailable) as exc:
            h.adapter.list_open_pulls(h.repo)
        assert exc.value.source == h.adapter.source

    def test_check_status_writes_once_and_updates_in_place(self, h):
        """Keyed by (repo, sha, name), exactly as GitHub keys check-runs."""
        if not h.adapter.supports_checks:
            pytest.skip("declares no neutral-capable check model")
        sha = "a" * 40
        first = h.adapter.set_check_status(h.repo, sha, "ledger/siblings", "neutral", "t1", "s1")
        second = h.adapter.set_check_status(h.repo, sha, "ledger/siblings", "success", "t2", "s2")
        assert first == second, "a re-run must update the same check, not add one"
        store = h.check_store()
        assert len(store) == 1
        assert store[0] == ("success", "t2")

    def test_unsupported_check_raises_rather_than_pretending(self, h):
        """Silently succeeding on a forge that cannot express 'neutral' is how a
        green tick ends up on a PR nobody examined."""
        if h.adapter.supports_checks:
            pytest.skip("this adapter supports checks")
        with pytest.raises(ForgeUnavailable):
            h.adapter.set_check_status(h.repo, "a" * 40, "ledger/siblings", "neutral", "t", "s")

    def test_commits_ahead_counts_unique_commits(self, h):
        base, head, expected = h.ahead_case
        assert h.adapter.commits_ahead(h.repo, base, head) == expected

    def test_commits_ahead_on_an_unknown_ref_raises_rather_than_answering(self, h):
        """How an unknown change is detected. Returning 0 here would make a
        branch that does not exist look fully integrated."""
        base, _, _ = h.ahead_case
        with pytest.raises(ForgeUnavailable):
            h.adapter.commits_ahead(h.repo, base, "no/such/branch-xyz")

    def test_read_files_matches_read_file_and_omits_the_unreadable(self, h):
        """Batched reads must return exactly what one-at-a-time reads would --
        a faster path that returns different data is a correctness bug."""
        if not h.adapter.supports_tree:
            pytest.skip("cannot read trees")
        ref, path, expected = h.tree_case
        got = h.adapter.read_files(h.repo, ref, [path, "no-such-file.md"])
        assert got == {path: h.adapter.read_file(h.repo, ref, path)}
        assert got[path] == expected
        assert h.adapter.read_files(h.repo, ref, []) == {}

    def test_head_sha_agrees_with_list_branches(self, h):
        """The stored index trusts this to decide what to rescan. If it ever
        disagreed with the branch listing, a changed repo would be skipped."""
        b = h.adapter.list_branches(h.repo)[0]
        assert h.adapter.head_sha(h.repo, b.name) == b.head_sha

    def test_every_listed_branch_resolves(self, h):
        """A listed branch that cannot be read is a phantom. localgit used to
        list `origin` in every repo -- its rendering of origin/HEAD."""
        for b in h.adapter.list_branches(h.repo):
            assert h.adapter.head_sha(h.repo, b.name) == b.head_sha, b.name

    def test_head_sha_of_an_unknown_ref_raises(self, h):
        with pytest.raises(ForgeUnavailable):
            h.adapter.head_sha(h.repo, "no/such/branch-xyz")

    def test_commits_touching_answers_or_says_it_cannot(self, h):
        """The one method a forge API cannot implement.

        None means "this source cannot tell", and the caller must not read it
        as "nothing changed". A list, empty or not, is an answer.
        """
        got = h.adapter.commits_touching(h.repo, "main", "ABC-321", "1970-01-01")
        assert got is None or all(c.sha and c.subject for c in got)


class TestLocalGitPickaxe:
    """`commits_touching` on a real repository, which is the only place it works."""

    def test_it_finds_the_commit_that_introduced_the_name(self, localgit_harness):
        h = localgit_harness
        got = h.adapter.commits_touching(h.repo, "main", "x", "1970-01-01", ["f.txt"])
        assert [c.subject for c in got] == ["ABC-321 init"]

    def test_a_name_no_commit_touched_is_an_empty_answer_not_none(self, localgit_harness):
        h = localgit_harness
        assert h.adapter.commits_touching(h.repo, "main", "nothing_here", "1970-01-01") == []

    def test_commits_before_the_cutoff_are_not_counted(self, localgit_harness):
        h = localgit_harness
        assert h.adapter.commits_touching(h.repo, "main", "x", "2999-01-01", ["f.txt"]) == []

    def test_a_cutoff_git_would_silently_ignore_is_still_applied(self, localgit_harness):
        """`git log --since=2999-01-01` and `--since=not-a-date` both return
        the whole history: git drops a date it cannot parse, with no error.
        Trusting it turned "5 commits since the page" into "every commit"."""
        h = localgit_harness
        assert h.adapter.commits_touching(h.repo, "main", "x", "2999-01-01", ["f.txt"]) == []
        assert h.adapter.commits_touching(h.repo, "main", "x", "not-a-date", ["f.txt"]) is None


class TestDefaultBranchSource:
    """Where the default branch comes from depends on the layout, and getting
    it wrong is silent and total: every diff is computed against the wrong
    baseline."""

    def test_a_working_clones_checked_out_branch_is_not_its_default(self, localgit_harness):
        """Two repositories checked out on `stage` reported `stage` as their
        default, which turns the integration branch into the baseline and
        makes everything merged into it look already released."""
        import pathlib as _pl
        import subprocess
        h = localgit_harness
        repo = _pl.Path(h.adapter.root) / h.repo
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "work"], check=True,
                       capture_output=True)
        assert h.adapter.list_repos()[0].default_branch == "main"

    def test_a_mirror_owns_its_head(self, mirror_harness):
        assert mirror_harness.adapter.list_repos()[0].default_branch == "main"


class TestRefsFromDisk:
    """The stored index checks every repository's head on every query, to
    catch one that moved since the last sync. On 171 mirrors that was 171
    subprocesses and 3.4 of a 6 second answer. Git keeps the same answer in
    `packed-refs` and a handful of loose files."""

    def test_it_agrees_with_git_on_a_working_clone(self, localgit_harness):
        import subprocess
        h = localgit_harness
        p = h.adapter._path(h.repo)
        want = subprocess.run(["git", "-C", str(p), "rev-parse", "main"],
                              capture_output=True, text=True).stdout.strip()
        assert h.adapter.head_sha(h.repo, "main") == want

    def test_it_agrees_with_git_on_a_mirror(self, mirror_harness):
        import subprocess
        h = mirror_harness
        p = h.adapter._path(h.repo)
        want = subprocess.run(["git", "-C", str(p), "rev-parse", "main"],
                              capture_output=True, text=True).stdout.strip()
        assert h.adapter.head_sha(h.repo, "main") == want

    def test_a_packed_ref_is_found(self, localgit_harness):
        """After `git gc` or a fresh clone there are no loose refs at all."""
        import subprocess
        h = localgit_harness
        p = h.adapter._path(h.repo)
        before = h.adapter.head_sha(h.repo, "main")
        subprocess.run(["git", "-C", str(p), "pack-refs", "--all"], check=True, capture_output=True)
        h.adapter.refresh()
        assert h.adapter.head_sha(h.repo, "main") == before

    def test_a_loose_ref_wins_over_a_packed_one(self, localgit_harness):
        """Git's own rule: a ref written since the last pack shadows it."""
        import subprocess
        h = localgit_harness
        p = h.adapter._path(h.repo)
        subprocess.run(["git", "-C", str(p), "pack-refs", "--all"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(p), "commit", "-q", "--allow-empty", "-m", "ABC-321 more"],
                       check=True, capture_output=True)
        h.adapter.refresh()
        want = subprocess.run(["git", "-C", str(p), "rev-parse", "main"],
                              capture_output=True, text=True).stdout.strip()
        assert h.adapter.head_sha(h.repo, "main") == want

    def test_a_commit_ish_still_asks_git(self, localgit_harness):
        """A sha or `HEAD~1` is not a ref, and only git can resolve it."""
        h = localgit_harness
        sha = h.adapter.head_sha(h.repo, "main")
        assert h.adapter.head_sha(h.repo, sha) == sha

    def test_branch_names_come_from_the_same_read(self, mirror_harness):
        h = mirror_harness
        assert "main" in h.adapter._names(h.adapter._path(h.repo))


class TestAStaleLocalBase:
    """In a working clone the local `stage` is whatever was last checked out.
    Measured: a three-file change, briefed against a local `stage` far behind
    `origin/stage`, was credited with 86 unreleased commits and came out as
    decision_needed."""

    def test_the_freshest_copy_of_a_base_is_the_one_diffed_against(self, tmp_path):
        import subprocess
        repo = tmp_path / "root" / "leads"
        repo.mkdir(parents=True)
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t"); run("config", "user.name", "t")
        (repo / "a.txt").write_text("a")
        run("add", "."); run("commit", "-qm", "init")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        run("branch", "-q", "stage")                      # the stale local copy
        for n in range(3):                                # stage moves on, upstream
            (repo / f"s{n}.txt").write_text("s")
            run("add", "."); run("commit", "-qm", f"unreleased {n}")
        run("update-ref", "refs/remotes/origin/stage", "HEAD")
        run("checkout", "-q", "-b", "feat/x")             # cut from the fresh stage
        (repo / "mine.txt").write_text("mine")
        run("add", "."); run("commit", "-qm", "the change itself")
        run("checkout", "-q", "main")
        run("reset", "-q", "--hard", "refs/remotes/origin/main")

        g = LocalGitAdapter(tmp_path / "root")
        assert g.commits_ahead("leads", "stage", "feat/x") == 1
        assert [f.path for f in g.changed_files("leads", "stage", "feat/x")] == ["mine.txt"]
