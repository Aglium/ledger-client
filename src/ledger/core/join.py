"""The ticket join: given one change, which OTHER in-flight work shares its ticket?

Pure functions over plain dataclasses so the logic is testable without a
database, a forge, or a model provider. This is the whole of P0-ticket's
intelligence.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Change:
    """One unit of in-flight work: an open PR, or a branch inside the liveness window."""

    repo: str
    ref: str
    tickets: tuple[str, ...]
    kind: str = "branch"            # "branch" | "pr" | "integration"
    #: For kind="integration": which branch holds it (e.g. "stage"), and the sha.
    integration_ref: str | None = None
    sha: str | None = None
    pr_number: int | None = None
    title: str | None = None
    author: str | None = None
    url: str | None = None
    updated_at: datetime | None = None
    open: bool = True


@dataclass(frozen=True)
class Sibling:
    change: Change
    shared_tickets: tuple[str, ...]


@dataclass
class JoinResult:
    """Siblings plus an explicit completeness statement.

    `partial` is not cosmetic. If one forge was unavailable, returning a short
    list silently is a false negative in a tool whose entire job is telling you
    what you cannot see. Callers must render the caveat.
    """

    subject: Change
    siblings: list[Sibling] = field(default_factory=list)
    partial: bool = False
    missing_sources: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.partial


#: Words that say what kind of branch it is, never what it is about. A pair
#: sharing only these shares nothing.
_BRANCH_NOISE = frozenset("""
feat feature features fix fixes bugfix hotfix chore docs doc test tests ci spec
refactor wip tmp temp release releases land port stage staging main master
develop development prod production update updates upgrade bump add adds remove
removes new old next init setup cleanup revert merge rebase draft
""".split())

#: Short tokens are initials and version fragments. Four is where real subject
#: words start: `comms`, `token`, `missing`, `documents`.
_MIN_WORD = 4

_WORD = re.compile(r"[^a-z0-9]+")
_TICKETISH = re.compile(r"\A[a-z]{2,5}-?\d+\Z", re.I)


def branch_words(ref: str) -> set[str]:
    """What a branch name says it is about, minus how it says it.

    `feat/xy-525-announce-on-crm-link` is about announcing on a crm link; the
    `feat` and the `xy-525` are metadata the rest of the system already reads.
    """
    out = set()
    for w in _WORD.split((ref or "").lower()):
        if len(w) < _MIN_WORD or w in _BRANCH_NOISE or _TICKETISH.match(w):
            continue
        out.add(w)
    return out


def shared_words(a: str, b: str) -> list[str]:
    """Subject words two branch names have in common, as evidence."""
    return sorted(branch_words(a) & branch_words(b))


def find_siblings(
    subject: Change,
    population: list[Change],
    *,
    cross_repo_only: bool = True,
    partial: bool = False,
    missing_sources: tuple[str, ...] = (),
) -> JoinResult:
    """Changes sharing >=1 ticket with `subject`.

    `cross_repo_only` is the default because same-repo siblings are already
    visible to the reviewer -- they are in the same PR list. The value of this
    tool is entirely in the repos the reviewer is not looking at.
    """
    result = JoinResult(subject=subject, partial=partial, missing_sources=missing_sources)
    if not subject.tickets:
        return result

    want = set(subject.tickets)
    for cand in population:
        if not cand.open:
            continue
        if cand.repo == subject.repo and cand.ref == subject.ref:
            continue
        if cross_repo_only and cand.repo == subject.repo:
            continue
        shared = want.intersection(cand.tickets)
        if shared:
            ordered = tuple(t for t in subject.tickets if t in shared)
            result.siblings.append(Sibling(change=cand, shared_tickets=ordered))

    result.siblings.sort(key=lambda s: (s.change.repo, s.change.ref))
    return result
