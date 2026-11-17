"""Who a change is by, across the names one person goes by.

A pull request's author is a forge login; a branch's is the email its head
commit carries; one person signs commits from a laptop's default address on
one machine and GitHub's `noreply` address on another. Compared as strings,
the same person's service and front end were two people, and a token change
in four repositories was a chain of three.

What ties the names together is in the data already: a pull request carries
both its author's login and the email of the commit at its head. Every such
pair says that email is that login -- unless the same email shows up under
two logins on one forge (a shared bot, a pairing machine), and then it says
nothing. Under one login on GitHub and another on Bitbucket it is one person
with an account on each: a laptop's default address signed a front end's pull
request on one forge and a service's on the other, and the two were strangers.
"""

from __future__ import annotations

import re
from collections import defaultdict

#: `12345+login@users.noreply.github.com` and the older `login@users.noreply.github.com`.
_NOREPLY = re.compile(r"^(?:\d+\+)?([^@\s]+)@users\.noreply\.github\.com$", re.I)


def _norm(name: str | None) -> str | None:
    name = (name or "").strip().lower()
    return name or None


def forge_of(c) -> str | None:
    """The forge a pull request is on, from its web address; None when unknown."""
    url = getattr(c, "url", None) or ""
    m = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)", url.strip(), re.I)
    return m.group(1).lower() if m else None


def noreply_login(email: str | None) -> str | None:
    m = _NOREPLY.match((email or "").strip())
    return m.group(1).lower() if m else None


def _one_per_forge(logins: dict[str, set]) -> bool:
    """At most one of these logins on any forge; a login on an unknown forge
    (None) counts on every forge."""
    known = {f for fs in logins.values() for f in fs if f is not None} or {None}
    return all(sum(1 for fs in logins.values() if f in fs or None in fs) <= 1 for f in known)


class People:
    """Names that are one person, learned from changes that carry two of them."""

    def __init__(self, changes=()):
        logins_of: dict[str, dict[str | None, set[str]]] = defaultdict(lambda: defaultdict(set))
        forges_of: dict[str, set[str | None]] = defaultdict(set)
        for c in changes:
            login, email = _norm(getattr(c, "login", None)), _norm(getattr(c, "author", None))
            if login and email and email != login:
                logins_of[email][forge_of(c)].add(login)
                forges_of[login].add(forge_of(c))
        # An email with two logins on one forge is nobody's; a pull request
        # whose forge is unknown counts on every forge.
        links = {e: set().union(*by_forge.values()) for e, by_forge in logins_of.items()
                 if _one_per_forge({lg: forges_of[lg] for ls in by_forge.values() for lg in ls})}
        # One person is every name the links reach -- unless that reaches two
        # logins on one forge: two emails each tied to `ann` and `bob` on
        # GitHub through one Bitbucket login made them one person. Such a
        # group's links are dropped and each name stands alone.
        parent: dict[str, str] = {}

        def root(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for e, logins in links.items():
            for lg in logins:
                parent[root(lg)] = root(e)
        groups: dict[str, set[str]] = defaultdict(set)
        for x in list(parent):
            groups[root(x)].add(x)
        #: name -> every name of that person, itself included.
        self.person: dict[str, set[str]] = {}
        for members in groups.values():
            logins = {m: forges_of[m] for m in members if m in forges_of}
            if _one_per_forge(logins):
                for m in members:
                    self.person[m] = members

    def names(self, author: str | None, login: str | None = None) -> set[str]:
        """Every name the person behind `author` (and `login`) goes by."""
        out: set[str] = set()
        for n in (_norm(author), _norm(login)):
            if not n:
                continue
            out.add(n)
            person = noreply_login(n)
            if person:
                out.add(person)
        for n in list(out):
            out |= self.person.get(n, set())
        return out

    def same(self, a, b) -> bool:
        """Whether two changes are by one person."""
        mine = self.names(getattr(a, "author", None), getattr(a, "login", None))
        return bool(mine) and bool(mine & self.names(getattr(b, "author", None),
                                                     getattr(b, "login", None)))


def times_of(c) -> list:
    """When a change last moved: its head's commit, and its pull request's last update."""
    return [t for t in (getattr(c, "updated_at", None), getattr(c, "touched_at", None)) if t]


def apart(a_times, b_times) -> float | None:
    """The closest two moments of two changes, in seconds."""
    gaps = [abs(x.timestamp() - y.timestamp()) for x in a_times for y in b_times]
    return min(gaps) if gaps else None
