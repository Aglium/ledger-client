"""The VCS port.

One interface, one contract test suite, N implementations. This is the seam
that keeps `core/` free of forge vocabulary. Organisations routinely span
more than one forge, so more than one implementation is the normal case.

Only the methods P0-ticket needs are abstract. Later phases (diffs, checks)
extend this deliberately rather than shipping stubs nobody calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class ForgeUnavailable(RuntimeError):
    """A forge could not be reached or refused us.

    Raised rather than swallowed: a caller that silently treats an unreachable
    forge as an empty forge produces a false negative in a tool whose only job
    is surfacing what you cannot otherwise see.
    """

    def __init__(self, source: str, reason: str):
        self.source = source
        self.reason = reason
        super().__init__(f"{source} unavailable: {reason}")


@dataclass(frozen=True)
class RepoRef:
    full_name: str
    default_branch: str
    host: str
    url: str | None = None


@dataclass(frozen=True)
class BranchRef:
    repo: str
    name: str
    head_sha: str
    updated_at: datetime | None = None
    #: Who last committed on it. Best-effort: a git source gets this for free
    #: from the ref listing it already runs, and a forge would need a commit
    #: lookup per branch, so there it stays None. It is what lets a branch
    #: whose name carries no ticket key join to anything at all.
    author: str | None = None


@dataclass(frozen=True)
class CommitRef:
    """One commit on an integration branch that has not reached the default branch."""

    sha: str
    subject: str
    author: str | None = None
    when: datetime | None = None
    merged_ref: str | None = None      # topic branch name, when this is a merge commit
    #: The rest of the message. Where the ticket key lives when the branch name
    #: and the subject do not carry one: `Refs: XY-525` in a trailer, or a line
    #: of explanation. Measured on a real estate -- 29% of branches name a
    #: ticket, 74% mention one in their commits -- so this is most of the join's
    #: input, not a detail.
    body: str = ""


@dataclass(frozen=True)
class ChangedFile:
    """One file in a diff. `body` is the hunk text, not the whole file."""

    path: str
    body: str = ""


@dataclass(frozen=True)
class PullRef:
    repo: str
    number: int
    title: str
    head_ref: str
    base_ref: str
    head_sha: str
    author: str | None = None
    url: str | None = None
    updated_at: datetime | None = None
    state: str = "open"
    #: The description. Both forges return it in the list call that fetches
    #: everything else here, so reading it is free.
    body: str = ""
    #: Opened from another repository. Its branch name is that repository's,
    #: so this organisation's branch rules say nothing about it.
    fork: bool = False


class VcsAdapter(ABC):
    """Read-only at P0-ticket except for `upsert_comment`."""

    #: Stable identifier used in partial-result reporting, e.g. "github".
    source: str

    #: Whether this source can list and read files at a ref. Required by Layer 0
    #: card extraction, which reads the default branch rather than a diff.
    supports_tree: bool = False

    #: Whether this source can produce a diff for a change. Required by the
    #: overlap control arm; declared rather than assumed so a source that cannot
    #: is skipped loudly instead of contributing silent emptiness.
    supports_diffs: bool = False

    #: Whether this forge can carry a commit check with a real "neutral" state.
    #: Bitbucket's build-status model has only INPROGRESS/SUCCESSFUL/FAILED/STOPPED
    #: -- no neutral -- and collapsing neutral onto SUCCESSFUL would report a green
    #: tick for a PR the index never examined. That is the exact false confidence
    #: the check exists to avoid, so the capability is declared off rather than
    #: approximated.
    supports_checks: bool = False

    #: Whether this forge can carry a PR comment. Adding the local-git adapter
    #: forced this flag: the port had quietly assumed every source is a forge
    #: with pull requests. A read-only source is a first-class citizen -- it
    #: still feeds the join -- so the capability is declared, not faked.
    supports_comments: bool = True

    @abstractmethod
    def list_repos(self) -> list[RepoRef]: ...

    @abstractmethod
    def list_branches(self, repo: str, *, since: datetime | None = None) -> list[BranchRef]:
        """Branches with commits newer than `since` (the liveness window)."""

    @abstractmethod
    def list_open_pulls(self, repo: str) -> list[PullRef]: ...

    @abstractmethod
    def upsert_comment(self, repo: str, pull_number: int, marker: str, body: str) -> str:
        """Create or update the single comment identified by `marker`.

        Must update in place. A tool that appends a new comment on every push
        trains people to collapse the thread, which is the same as being muted.
        Returns the comment id.
        """

    def commits_between(self, repo: str, base: str, head: str, *,
                        limit: int | None = None,
                        exclude: tuple[str, ...] = ()) -> list[CommitRef]:
        """Commits in `head` that are not yet in `base`.

        This is how an integration branch becomes visible. Work merged to
        `stage` but not to `main` is in flight in the most meaningful sense --
        written, integrated, not yet released -- and modelling only topic
        branches misses all of it.

        It is also how a topic branch's ticket key is found when the branch
        name does not carry one, which is most of the time. `limit` caps the
        walk for that caller: a long-lived branch must not dominate the sweep.

        `exclude` names further refs whose commits are not this change's. A
        branch cut from an integration branch contains everything unreleased
        in the repository: one real branch had 501 commits and 34 ticket keys
        beyond the default branch, 33 of them other people's. Excluding the
        integration branches too leaves the branch's own work, which is the
        only thing its siblings should be found from.

        A source that cannot do this raises, and the caller degrades rather
        than reporting fewer links as if there were fewer.
        """
        raise ForgeUnavailable(self.source, "this source cannot enumerate commits")

    def list_tree(self, repo: str, ref: str) -> list[str]:
        """Every file path at `ref`. Paths only -- reading is a separate, costed step."""
        raise ForgeUnavailable(self.source, "this source cannot list trees")

    def read_file(self, repo: str, ref: str, path: str) -> str:
        """File contents at `ref`. Binary or missing files raise rather than
        returning empty: 'unreadable' and 'empty' are different facts."""
        raise ForgeUnavailable(self.source, "this source cannot read files")

    def head_sha(self, repo: str, ref: str) -> str:
        """The commit `ref` currently points at.

        How the stored index knows whether a repo needs rescanning: if its
        default branch still points where it did at the last scan, nothing in
        it can have changed. The default works from list_branches, so every
        adapter has it; adapters with a cheaper lookup should override.
        """
        for b in self.list_branches(repo):
            if b.name == ref:
                return b.head_sha
        raise ForgeUnavailable(self.source, f"{repo}: no branch {ref!r}")

    def read_files(self, repo: str, ref: str, paths: list[str]) -> dict[str, str]:
        """Read many files at once. Unreadable ones are simply absent.

        The default loops over read_file. Adapters that can batch should: reading
        a whole repo one file per round trip is what made a single brief take
        forty seconds.
        """
        out: dict[str, str] = {}
        for p in paths:
            try:
                out[p] = self.read_file(repo, ref, p)
            except ForgeUnavailable:
                continue
        return out

    def merged_into(self, repo: str, into: str, ref: str, default: str) -> bool:
        """Whether `ref` was merged into `into` as a change of its own, rather
        than cut from it.

        Both have no commits beyond `into`. A branch merged with a merge commit
        sits off `into`'s first-parent line; a branch cut from `into` (or `into`
        itself) sits on it. A source that cannot tell raises, and the caller
        assumes "cut from", which reads the change as before.
        """
        raise ForgeUnavailable(self.source, "this source cannot tell a merged branch from a cut one")

    def commits_ahead(self, repo: str, base: str, head: str) -> int:
        """Commits in `head` that are not in `base`.

        Used to find which branch a change is really built on. Also the cheapest
        way to establish that a ref exists at all: an unknown ref raises.
        """
        raise ForgeUnavailable(self.source, "this source cannot count commits")

    def changed_files(self, repo: str, base: str, head: str) -> list[ChangedFile]:
        """Files changed between `base` and `head`, with hunk bodies."""
        raise ForgeUnavailable(self.source, "this source cannot produce diffs")

    def grep_paths(self, repo: str, ref: str, needles: list[str], paths: list[str] | None = None) -> list[str]:
        """Files at `ref` containing any of `needles`, case-insensitively.

        Finding where an identifier is used should not mean reading a large
        repository in full. The default reads files; git answers directly.
        """
        paths = paths if paths is not None else self.list_tree(repo, ref)
        low = [n.lower() for n in needles if n]
        texts = self.read_files(repo, ref, paths)
        return [p for p, t in texts.items() if any(n in t.lower() for n in low)]

    def working_changes(self, repo: str) -> list[ChangedFile] | None:
        """Uncommitted changes in a checked-out clone, as a diff.

        An agent session spends most of its life here: files edited, nothing
        committed yet. A real session changed nine files and 423 lines, and
        every answer about that branch said "no commits beyond master, so it
        touches no contracts" -- true, and useless, because the work existed
        and was invisible.

        None means this source has no working tree to read, which every forge
        API does not.
        """
        return None

    def commits_touching(self, repo: str, ref: str, name: str, since,
                         paths: list[str] | None = None) -> list[CommitRef] | None:
        """Commits after `since` whose diff changed a line naming `name`.

        This is how far documentation has fallen behind. Counting commits to
        the *file* answers the wrong question -- one `schema.sql` holds every
        table, and on a real estate that made 82% of pages look stale. What a
        person means by "the page is behind" is that lines naming this
        contract changed after the page was written.

        None means this source cannot tell, which is not the same as "none",
        and must never be reported as up to date.
        """
        return None

    def changed_paths(self, repo: str, base: str, head: str) -> list[str]:
        """Which files changed, without their contents.

        Scanning every live branch for decision documents only needs the names.
        Pulling whole diff bodies to discover a branch touches no ADR is the
        expensive way to learn nothing, so a source that can answer cheaply
        (git: --name-only) overrides this.
        """
        return [f.path for f in self.changed_files(repo, base, head)]

    def set_check_status(
        self, repo: str, head_sha: str, name: str, conclusion: str, title: str, summary: str
    ) -> str:
        """Create or update the named check on `head_sha`.

        Not abstract: a source without a neutral-capable check model declares
        `supports_checks = False` and is never asked. Raising here rather than
        silently succeeding keeps a misconfiguration loud.
        """
        raise ForgeUnavailable(self.source, "this source has no neutral-capable check model")
