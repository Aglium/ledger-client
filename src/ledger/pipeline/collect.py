"""Gather in-flight work from every configured forge into `Change` objects.

T20 lives here: if one forge is unavailable we return what we have AND say so.
Neither of the two easy behaviours is acceptable -- failing the whole run makes
the tool fragile, and silently returning a short list makes it lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ledger.adapters.vcs.base import ForgeUnavailable, VcsAdapter
from ledger.core.branches import DEFAULT_RULES, BranchRules, expand_integration
from ledger.core.join import Change
from ledger.core.tickets import extract_tickets


@dataclass
class Collection:
    changes: list[Change] = field(default_factory=list)
    #: Forge metadata for every repo reached, so the store can upsert it without
    #: a second round of API calls.
    repo_refs: dict = field(default_factory=dict)
    #: Which adapter each repo came from, for provenance on the stored row.
    repo_sources: dict = field(default_factory=dict)
    repos_seen: int = 0
    partial: bool = False
    missing_sources: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)
    #: Branches and pull requests left out by `branches.watch`/`branches.ignore`.
    branches_ignored: int = 0


#: How far back to read a branch's own commits for a ticket key. A topic
#: branch is a handful of commits; a long-lived one must not dominate the
#: sweep, and the key is in the recent work either way.
COMMIT_SCAN = 80

#: Above this, the commits are not what this change is about. A branch
#: carrying twenty-three tickets is not a change under twenty-three tickets;
#: it is a parallel line of development that has accumulated history, and
#: joining on all of it makes it a sibling of everything.
#:
#: The cut is where the measured distribution has its gap, not a round number.
#: Of 120 branches carrying a ticket: 89% carry six or fewer, one carries
#: seven and one eight, **nothing carries nine or ten**, and the eleven above
#: that are the long-lived tail -- `feature/stage-mindbox-cohort` with 389
#: commits on neither `main` nor `stage`, and the `stage` branches themselves.
#:
#: Tickets in the branch *name* are kept regardless: a name is a claim
#: somebody made about the work, where a long history is only what the branch
#: has been near.
MAX_COMMIT_TICKETS = 8


def _commit_tickets(adapter: VcsAdapter, repo: str, base: str, ref: str,
                    project_keys, exclude: tuple[str, ...] = ()) -> set[str]:
    """Ticket keys named in a branch's **own** commits, subjects and bodies both.

    The body matters on its own: a key the subject omits often sits in a
    trailer (`Refs: XY-525`).

    "Own" is the whole difficulty. A branch cut from an integration branch
    contains everything unreleased in the repository, so `default..branch` is
    not its work: one real branch had 501 commits and 34 ticket keys that way,
    33 of them other people's, and it inherited siblings under all of them.
    The integration branches are excluded as well, which leaves the commits
    this branch actually added -- and zero of them for a branch sitting at
    `stage`, which is the right answer, not a gap.

    Returns an empty set rather than raising: a source that cannot read
    commits makes this signal absent, which the caller reports as absent
    instead of as "no siblings".
    """
    try:
        commits = adapter.commits_between(repo, base, ref, limit=COMMIT_SCAN,
                                          exclude=exclude)
    except (ForgeUnavailable, NotImplementedError, TypeError):
        return set()
    text = " ".join(f"{c.subject} {c.body}" for c in commits)
    found = set(extract_tickets(text, project_keys))
    return found if len(found) <= MAX_COMMIT_TICKETS else set()


def collect(
    adapters: list[VcsAdapter],
    project_keys: tuple[str, ...],
    *,
    liveness_days: int = 30,
    integration_branches: tuple[str, ...] = (),
    exclude_repos: tuple[str, ...] = (),
    branches: BranchRules = DEFAULT_RULES,
    now: datetime | None = None,
) -> Collection:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=liveness_days)
    out = Collection()
    missing: list[str] = []

    for adapter in adapters:
        try:
            repos = adapter.list_repos()
        except ForgeUnavailable as e:
            missing.append(adapter.source)
            out.errors.append(str(e))
            continue

        for repo in repos:
            if repo.full_name in exclude_repos:
                continue
            out.repos_seen += 1
            out.repo_refs[repo.full_name] = repo
            out.repo_sources[repo.full_name] = adapter.source
            repo_degraded = False

            try:
                for p in adapter.list_open_pulls(repo.full_name):
                    if not branches.allows(p.head_ref):
                        out.branches_ignored += 1
                        continue
                    # The branch name, the PR title and the description all
                    # carry ticket ids, and they disagree often enough to be
                    # worth unioning. The description is free: both forges
                    # return it in the same call that fetched the rest.
                    tickets = extract_tickets(
                        f"{p.head_ref} {p.title} {p.body}", project_keys)
                    out.changes.append(
                        Change(
                            repo=repo.full_name, ref=p.head_ref, tickets=tuple(tickets),
                            kind="pr", pr_number=p.number, title=p.title, author=p.author,
                            url=p.url, updated_at=p.updated_at, open=True, sha=p.head_sha,
                        )
                    )
            except ForgeUnavailable as e:
                repo_degraded = True
                out.errors.append(str(e))

            integ_here = expand_integration(integration_branches, ())
            try:
                pr_refs = {c.ref for c in out.changes if c.repo == repo.full_name}
                live = adapter.list_branches(repo.full_name, since=since)
                integ_here = expand_integration(integration_branches, [b.name for b in live])
                for b in live:
                    if b.name in pr_refs or b.name == repo.default_branch:
                        continue
                    if b.name in integ_here:
                        continue        # read below, as integrated work
                    if not branches.allows(b.name):
                        out.branches_ignored += 1
                        continue
                    tickets = extract_tickets(b.name, project_keys)
                    # What the branch name does not say, the commits do.
                    # Measured on this estate: 29% of live branches name a
                    # ticket, 74% mention one in their commit messages, and
                    # reading them takes the cross-repo ticket join from 6
                    # linked pairs to 378 -- from 11 branches with a sibling
                    # to 64. The join itself does not change; only its input.
                    #
                    # A source that cannot enumerate commits (every forge
                    # adapter: it would be a call per branch) degrades to
                    # exactly the previous behaviour.
                    tickets = sorted(set(tickets) | _commit_tickets(
                        adapter, repo.full_name, repo.default_branch, b.name, project_keys,
                        exclude=tuple(x for x in integ_here
                                      if x != b.name and x != repo.default_branch)))
                    if not tickets and not b.author:
                        # A branch with neither a ticket key nor an author
                        # cannot participate in any join, and dropping it here
                        # is what keeps ~700 refs from becoming ~700 rows of
                        # noise.
                        #
                        # With an author it can, and must. Measured on the
                        # estate this was written against: of 229 branches
                        # with a commit in the last 30 days, 40 carry a ticket
                        # key -- so requiring one made the ledger blind to 83%
                        # of active work, which is the complaint that
                        # motivated the author join in the first place
                        # ("linking cannot depend on ticket ids: branch names
                        # and commit messages here often do not follow that
                        # convention"). A ticketless row is reachable only
                        # from that join: nothing else can match it.
                        #
                        # Source-dependent, and that is a defect: GitHub's
                        # branch list carries no author (a commit lookup per
                        # branch), so on a forge-only deployment this drops
                        # every ticketless branch while on a mirror it drops
                        # almost none. The honest fix is to keep a branch that
                        # can join by *some* signal, which needs the diff
                        # signals to exist first.
                        continue
                    out.changes.append(
                        Change(
                            repo=repo.full_name, ref=b.name, tickets=tuple(tickets),
                            kind="branch", author=b.author, updated_at=b.updated_at,
                            open=True, sha=b.head_sha,
                        )
                    )
            except ForgeUnavailable as e:
                repo_degraded = True
                out.errors.append(str(e))

            # Integrated-but-unreleased work. A topic branch merged to `stage`
            # has vanished from the open-branch list and is absent from `main`,
            # so without this it is invisible to the whole system -- which is
            # exactly the work most likely to collide with what is open now.
            known = {(c.repo, c.ref) for c in out.changes}
            for integ in integ_here:
                if integ == repo.default_branch:
                    continue
                try:
                    commits = adapter.commits_between(repo.full_name, repo.default_branch, integ)
                except ForgeUnavailable:
                    continue    # the branch simply may not exist in this repo
                for commit in commits:
                    # A merge names its topic branch; a direct commit does not,
                    # so it is identified by sha rather than pretending to be a
                    # branch that never existed.
                    ref = commit.merged_ref or commit.sha[:12]
                    tickets = extract_tickets(f"{ref} {commit.subject}", project_keys)
                    if not tickets:
                        continue
                    key = (repo.full_name, ref)
                    if key in known:
                        continue
                    known.add(key)
                    out.changes.append(
                        Change(
                            repo=repo.full_name, ref=ref, tickets=tuple(tickets),
                            kind="integration", title=commit.subject, author=commit.author,
                            updated_at=commit.when, open=True,
                            integration_ref=integ, sha=commit.sha,
                        )
                    )

            if repo_degraded and adapter.source not in missing:
                missing.append(adapter.source)

    out.missing_sources = tuple(missing)
    out.partial = bool(missing)
    return out
