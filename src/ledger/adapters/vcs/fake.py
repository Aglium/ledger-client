"""In-memory adapter. Exists so the contract suite has a reference implementation
to check itself against, and so pipeline tests need no network."""

from __future__ import annotations

from datetime import datetime

from .base import (
    BranchRef, ChangedFile, CommitRef, ForgeUnavailable, PullRef, RepoRef, VcsAdapter,
)


class FakeAdapter(VcsAdapter):
    source = "fake"
    supports_checks = True
    supports_diffs = True
    supports_tree = True

    def __init__(self, repos=None, branches=None, pulls=None, diffs=None, tree=None, commits=None, ahead=None, touching=None, working=None, *, unavailable: bool = False):
        self._repos = repos or []
        self._branches = branches or {}
        self._pulls = pulls or {}
        self._diffs = diffs or {}
        self._tree = tree or {}
        self._commits = commits or {}
        self._ahead = ahead or {}
        #: {(repo, contract name): [CommitRef]} -- what a pickaxe search finds.
        #: Absent means this source cannot tell, which is the default: only a
        #: local clone can answer it.
        self._touching = touching
        #: {repo: [ChangedFile]} -- uncommitted work. None (the default) means
        #: this source has no working tree, as no forge API has.
        self._working = working
        self._unavailable = unavailable
        self.comments: dict[tuple[str, int, str], str] = {}
        self.checks: dict[tuple[str, str, str], tuple[str, str, str]] = {}

    def _guard(self):
        if self._unavailable:
            raise ForgeUnavailable(self.source, "simulated outage")

    def working_changes(self, repo):
        self._guard()
        if self._working is None:
            return None
        return list(self._working.get(repo, []))

    def commits_touching(self, repo, ref, name, since, paths=None):
        self._guard()
        if self._touching is None:
            return None
        # `since` is honoured here too: a fake that answers more freely than
        # git does would hide exactly the bugs this stands in for.
        return [c for c in self._touching.get((repo, name), [])
                if not (c.when and since) or c.when.date().isoformat() > str(since)]

    def list_repos(self) -> list[RepoRef]:
        self._guard()
        return list(self._repos)

    def list_branches(self, repo: str, *, since: datetime | None = None) -> list[BranchRef]:
        self._guard()
        out = list(self._branches.get(repo, []))
        if since is not None:
            out = [b for b in out if b.updated_at is None or b.updated_at >= since]
        return out

    def list_open_pulls(self, repo: str) -> list[PullRef]:
        self._guard()
        return [p for p in self._pulls.get(repo, []) if p.state == "open"]

    def upsert_comment(self, repo: str, pull_number: int, marker: str, body: str) -> str:
        self._guard()
        # The marker is embedded in the stored body, exactly as the real adapters
        # do it. The shared contract suite caught this divergence on its first
        # run: a fake that stores a different shape than production is a fake
        # that lets production bugs through.
        key = (repo, pull_number, marker)
        self.comments[key] = f"{body}\n\n<!-- {marker} -->"
        return f"fake-comment-{repo}-{pull_number}"

    def set_check_status(
        self, repo: str, head_sha: str, name: str, conclusion: str, title: str, summary: str
    ) -> str:
        self._guard()
        # Keyed by (repo, sha, name) exactly as GitHub keys check-runs, so a
        # re-run updates rather than accumulating.
        self.checks[(repo, head_sha, name)] = (conclusion, title, summary)
        return f"fake-check-{repo}-{head_sha[:7]}-{name}"

    def changed_files(self, repo: str, base: str, head: str) -> list[ChangedFile]:
        self._guard()
        return list(self._diffs.get(repo, []))

    def list_tree(self, repo: str, ref: str) -> list[str]:
        self._guard()
        return sorted(self._tree.get(repo, {}))

    def read_file(self, repo: str, ref: str, path: str) -> str:
        self._guard()
        files = self._tree.get(repo, {})
        if path not in files:
            raise ForgeUnavailable(self.source, f"{repo}:{path} not found")
        return files[path]

    def commits_between(self, repo: str, base: str, head: str, *,
                        limit: int | None = None,
                        exclude: tuple[str, ...] = ()) -> list[CommitRef]:
        self._guard()
        out = list(self._commits.get((repo, head), []))
        return out[:limit] if limit else out

    #: {(repo, into, ref): bool} for `merged_into`; unset pairs cannot be told.
    merged: dict = {}

    def merged_into(self, repo: str, into: str, ref: str, default: str) -> bool:
        key = (repo, into, ref)
        if key not in self.merged:
            raise ForgeUnavailable(self.source, "not configured")
        return self.merged[key]

    def commits_ahead(self, repo: str, base: str, head: str) -> int:
        """Configured per (repo, base, head). An unconfigured pair is an unknown
        ref, which is what the real adapters raise for, so this does too."""
        self._guard()
        key = (repo, base, head)
        if key not in self._ahead:
            raise ForgeUnavailable(self.source, f"{repo}: cannot resolve {base}..{head}")
        return self._ahead[key]
