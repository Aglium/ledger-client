"""What an in-flight change does, said to a reader who cannot open it.

A brief used to name the other change and stop: a repository, a branch and an
author. From the feedback that asked for more, after a review built on such a
pointer was checked by the branch's own author:

    "The brief's fact was right, the model's conclusion from it was inverted.
    The brief gave only a branch name and author... The reviewing model read
    the commit list, saw [a commit title], and cited it as proof."

and, in order of value, what was missing: "what the in-flight branch DOES, not
only that it exists", "the shared contract that links the repos" with "the
field that crosses the boundary", "an evidence-level marker", and "a prompt
for the right question".

So each in-flight item carries four short statements, all derived here from
the two diffs and nothing else:

    effect     what its diff adds, removes and changes -- contracts by name
               and direction, table operations, payload keys
    relation   which way it faces the change being briefed, over which
               contract, and which names cross between them
    evidence   how much of this anybody has read
    ask        the one question worth putting to its author

Names, directions and roles only. No line of the other repository's code is
quoted: the reader may not be allowed to see it, and an estate this size is
not checked out anywhere.

Deterministic, like everything a gate may read. A model can say more -- that a
branch "re-enters LEAD as a new cycle" is behaviour, not a name -- and where
one is configured its sentence can replace `effect`; nothing else here depends
on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ledger.core.contracts import (
    NOT_A_TABLE, ContractRef, Edge, Surface, discover_in_text, http_equivalent,
)

#: Shown per group. Past this a line is a list, and a list is skipped.
SHOWN = 3

_KEY = re.compile(r"['\"]([a-zA-Z_][a-zA-Z0-9_]{2,40})['\"]\s*:")
_ATTR = re.compile(r"^\s*([a-z_][a-z0-9_]{2,40})\s*:\s*[A-Za-z_\[\]|. ]+(?:=|$)")
_COLUMN_OP = re.compile(
    r"\b(?P<op>add_column|drop_column|alter_column)\s*\(\s*['\"](?P<t>\w+)['\"]\s*,\s*"
    r"(?:\w+\.)?(?:Column\s*\(\s*)?['\"](?P<c>\w+)['\"]", re.I)
_TABLE_CREATE = re.compile(
    r"\bcreate_table\s*\(\s*['\"](?P<t>\w+)|\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r"[`\"\[]?(?P<t2>\w+)", re.I)
_TABLE_DROP = re.compile(
    r"\bdrop_table\s*\(\s*['\"](?P<t>\w+)|\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`\"\[]?(?P<t2>\w+)",
    re.I)

#: Keys every payload has. Sharing one links nothing.
_PLAIN_KEYS = frozenset("""
id name type data value key status title description created_at updated_at
version default required properties items enum format example summary tags
""".split())


@dataclass
class Delta:
    """One change's diff, reduced to what it names and which way.

    `added` and `removed` are contract edges whose lines exist on one side of
    the diff only; `changed` are named on both, which is a line that was
    edited rather than a contract that came or went.
    """

    added: dict[ContractRef, set[Edge]] = field(default_factory=dict)
    removed: dict[ContractRef, set[Edge]] = field(default_factory=dict)
    changed: dict[ContractRef, set[Edge]] = field(default_factory=dict)
    #: Table operations, in words: "creates table `x`", "drops column `x.y`".
    tables: list[str] = field(default_factory=list)
    keys_added: set[str] = field(default_factory=set)
    keys_removed: set[str] = field(default_factory=set)
    files: int = 0

    @property
    def contracts(self) -> dict[ContractRef, set[Edge]]:
        out: dict[ContractRef, set[Edge]] = {}
        for part in (self.added, self.removed, self.changed):
            for c, edges in part.items():
                out.setdefault(c, set()).update(edges)
        return out

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.tables
                    or self.keys_added or self.keys_removed)


def _sides(body: str) -> tuple[str, str]:
    """A diff body as the text it adds and the text it removes."""
    plus, minus = [], []
    for line in (body or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
    return "\n".join(plus), "\n".join(minus)


def _edges(repo: str, path: str, text: str) -> dict[ContractRef, set[Edge]]:
    out: dict[ContractRef, set[Edge]] = {}
    for e in discover_in_text(repo, path, text):
        out.setdefault(e.contract, set()).add(e.edge)
    return out


def _keys(text: str) -> set[str]:
    found = {m.group(1) for m in _KEY.finditer(text)}
    found |= {m.group(1) for line in text.splitlines() if (m := _ATTR.match(line))}
    return {k for k in found if k.lower() not in _PLAIN_KEYS}


def delta_of(repo: str, files, *, extra: dict[ContractRef, set[Edge]] | None = None) -> Delta:
    """Reduce a change's diff to a `Delta`. Same input, same output.

    `files` is anything with `.path` and `.body`, the body being diff lines
    that start with `+` or `-`. `extra` adds contracts the caller found by
    other means -- a topic reached through a bound constant rather than its
    literal -- which are known to be touched and not known to be added or
    removed, so they land in `changed`.
    """
    d = Delta()
    for f in files:
        d.files += 1
        plus, minus = _sides(f.body or "")
        a, r = _edges(repo, f.path, plus), _edges(repo, f.path, minus)
        for c in set(a) | set(r):
            if c in a and c in r:
                d.changed.setdefault(c, set()).update(a[c] | r[c])
            elif c in a:
                d.added.setdefault(c, set()).update(a[c])
            else:
                d.removed.setdefault(c, set()).update(r[c])
        for text, sign in ((plus, "+"), (minus, "-")):
            for m in _COLUMN_OP.finditer(text):
                op = m.group("op").lower()
                # A removed `add_column` line is a migration being rewritten,
                # not a column being dropped; only added lines say what the
                # change does to the table.
                if sign == "+":
                    verb = {"add_column": "adds", "drop_column": "drops",
                            "alter_column": "alters"}[op]
                    d.tables.append(f"{verb} column `{m.group('t')}.{m.group('c')}`")
            if sign == "+":
                for verb, rx in (("creates", _TABLE_CREATE), ("drops", _TABLE_DROP)):
                    for m in rx.finditer(text):
                        name = (m.group("t") or m.group("t2")).lower()
                        # `DROP TABLE IF EXISTS x` once produced a table
                        # called `if`, on a real diff, in a real brief.
                        if len(name) >= 2 and name not in NOT_A_TABLE:
                            d.tables.append(f"{verb} table `{name}`")
        ka, kr = _keys(plus), _keys(minus)
        d.keys_added |= ka - kr
        d.keys_removed |= kr - ka
    # A key added in one file and removed in another has moved.
    moved = d.keys_added & d.keys_removed
    d.keys_added -= moved
    d.keys_removed -= moved
    # Present on both sides across files is an edit as well.
    for c in set(d.added) & set(d.removed):
        d.changed.setdefault(c, set()).update(d.added.pop(c) | d.removed.pop(c))
    for c, edges in (extra or {}).items():
        if c not in d.added and c not in d.removed:
            d.changed.setdefault(c, set()).update(edges)
    d.tables = list(dict.fromkeys(d.tables))
    return d


def _edge_of(edges: set[Edge]) -> Edge:
    """One direction for a contract named several ways. Both ways is a mention."""
    real = edges - {Edge.MENTIONS}
    return next(iter(real)) if len(real) == 1 else Edge.MENTIONS


def _phrase(c: ContractRef, edges: set[Edge], verb: str) -> str:
    """`starts publishing topic `x``, `removes route `/y``, `changes table `z``."""
    e = _edge_of(edges)
    name = f"`{c.name}`"
    if c.surface == Surface.TABLE:
        return {"adds": f"starts writing table {name}", "removes": f"stops naming table {name}",
                "changes": f"changes lines naming table {name}"}[verb]
    if c.surface == Surface.HTTP:
        what = {Edge.PUBLISHES: "route", Edge.CONSUMES: "call to", Edge.MENTIONS: "mention of"}[e]
        return {"adds": f"adds {what} {name}", "removes": f"REMOVES {what} {name}",
                "changes": f"changes {what} {name}"}[verb]
    doing = {Edge.PUBLISHES: "publishing", Edge.CONSUMES: "consuming", Edge.MENTIONS: "naming"}[e]
    return {"adds": f"starts {doing} topic {name}", "removes": f"STOPS {doing} topic {name}",
            "changes": f"changes how it is {doing} topic {name}"
            if e != Edge.MENTIONS else f"changes lines naming topic {name}"}[verb]


def _some(items: list[str], shown: int = SHOWN) -> str:
    items = sorted(items)
    more = f" (+{len(items) - shown} more)" if len(items) > shown else ""
    return ", ".join(items[:shown]) + more


def effect_line(d: Delta) -> str:
    """What the change does, as one line. Removals are said as loudly as
    additions: the review this exists for needed to know that an emit call
    site had been REMOVED and replaced by one elsewhere, and saw neither."""
    nothing = (f"names no topic, route or table in its diff ({d.files} file(s) changed) — "
               "its effect is in behaviour, which this cannot summarise")
    if d.empty:
        return nothing
    parts: list[str] = []
    # Removals first. They are what breaks a reader, and the end of a line is
    # where a renderer cuts.
    for verb, part in (("removes", d.removed), ("adds", d.added), ("changes", d.changed)):
        # Only what has a direction. A topic or route merely *named* is, on
        # real diffs, mostly not one: a replay over four past reviews showed
        # `c.id` and `signature.png` reported as topics and "+110 more"
        # mentions of routes from test files, and the reader did worse with
        # those lines than with none.
        said = [_phrase(c, e, verb) for c, e in part.items()
                if not (c.surface != Surface.TABLE and _edge_of(e) == Edge.MENTIONS)
                and not (c.surface == Surface.TABLE and any(f"`{c.name}`" in t or f"`{c.name}." in t
                                                           for t in d.tables))]
        if said:
            parts.append(_some(said))
    if d.tables:
        parts.append(_some(d.tables))
    keys = []
    if d.keys_removed:
        keys.append("removes field(s) " + _some([f"`{k}`" for k in d.keys_removed], 4))
    if d.keys_added:
        keys.append("adds field(s) " + _some([f"`{k}`" for k in d.keys_added], 4))
    # Field names alone say nothing a reader can act on, and there are
    # dozens of them in an ordinary diff.
    if not parts:
        return nothing
    parts += keys
    return "; ".join(parts)


@dataclass
class Shared:
    """A contract both changes name, and which way each faces it."""

    contract: ContractRef
    theirs: Edge
    yours: Edge
    #: Whether their diff takes the contract away rather than adding to it.
    removed: bool = False


def shared_contracts(theirs: Delta, yours: dict[ContractRef, set[Edge]]) -> list[Shared]:
    """Contracts named by both diffs. Routes match as endpoints, not strings."""
    out: list[Shared] = []
    for c, edges in theirs.contracts.items():
        for mine, my_edges in yours.items():
            if mine.surface != c.surface:
                continue
            same = (mine.name == c.name or
                    (c.surface == Surface.HTTP and http_equivalent(mine.name, c.name)))
            if same:
                out.append(Shared(c, _edge_of(edges), _edge_of(my_edges), removed=c in theirs.removed))
                break
    # What can break first: a contract being removed, then a real
    # producer/consumer pair, then two of the same, then mentions.
    def rank(s: Shared) -> tuple:
        pair = {s.theirs, s.yours} == {Edge.PUBLISHES, Edge.CONSUMES}
        return (not s.removed, not pair, Edge.MENTIONS in (s.theirs, s.yours),
                s.contract.surface.value, s.contract.name)
    out.sort(key=rank)
    return out


def _crossing(theirs: Delta, your_keys: set[str], known_fields: list[dict]) -> list[str]:
    """Field names that cross between the two changes.

    Strongest first: a name their diff changes that this change's diff also
    names; then a name their diff changes that the index already knows a
    reader or writer of on the shared contract.
    """
    their_keys = theirs.keys_added | theirs.keys_removed
    both = sorted(their_keys & your_keys)
    indexed = sorted({f["field"].split(".")[-1].rstrip("[]") for f in known_fields} & their_keys)
    return list(dict.fromkeys(both + indexed))[:4]


def relation_line(theirs: Delta, yours: dict[ContractRef, set[Edge]], *,
                  your_keys: set[str] | None = None, known_fields: list[dict] | None = None,
                  repo_role: str | None = None) -> tuple[str, Shared | None, list[str]]:
    """Which way the in-flight change faces this one.

    Returns the sentence, the contract it rests on (None when there is none)
    and the field names that cross, for `ask_line`.

    `repo_role` is what the index says the other REPOSITORY does with a
    contract this change touches -- "publishes topic `x`" -- used only when
    the two diffs share no name, and said as weaker than a shared name
    because it is about the repository rather than about this branch.
    """
    shared = shared_contracts(theirs, yours)
    if not shared:
        if repo_role:
            return (f"its repository {repo_role}, which this change touches, but this branch's "
                    "diff does not name it — related by repository, not shown to be by this "
                    "branch", None, [])
        # Not "unrelated". This used to say "treat it as unrelated until its
        # author says otherwise", and a replay of four past reviews showed
        # what that did: agents repeated it about the producer half of the
        # very message they were consuming. Two changes can be two halves of
        # one feature and share no topic, route or table name -- a rating
        # written in one service and read from a table in another.
        return ("Ledger found no topic, route or table named by both diffs. That is a limit of "
                "what it can match, not a sign they are unrelated: the link given above stands",
                None, [])
    s = shared[0]
    fields = _crossing(theirs, your_keys or set(), known_fields or [])
    kind = {Surface.TOPIC: "topic", Surface.HTTP: "endpoint", Surface.TABLE: "table"}[s.contract.surface]
    name = f"{kind} `{s.contract.name}`"
    if s.theirs == Edge.PUBLISHES and s.yours == Edge.CONSUMES:
        line = f"it produces → you consume {name}"
    elif s.theirs == Edge.CONSUMES and s.yours == Edge.PUBLISHES:
        line = f"it consumes ← you produce {name}"
    elif s.theirs == s.yours == Edge.PUBLISHES:
        line = (f"both write {name}" if s.contract.surface == Surface.TABLE
                else f"both sides produce {name}")
    elif s.theirs == s.yours == Edge.CONSUMES:
        line = f"both sides consume {name}"
    else:
        line = f"both name {name}; the direction is not clear from either diff"
    if s.removed:
        line += " — and its diff REMOVES its side of it"
    if fields:
        line += "; field(s) crossing: " + ", ".join(f"`{f}`" for f in fields)
    if len(shared) > 1:
        line += f" (+{len(shared) - 1} more shared contract(s))"
    return line, s, fields


def evidence_line(*, sha: str | None, where: str | None, base: str | None,
                  summarised: bool) -> str:
    """How much of this anybody has read.

    The marker the feedback asked for by name: "unmerged, not read by you; a
    commit title is not evidence, read the diff and trace the mechanism
    before citing it".
    """
    at = f" @{sha[:12]}" if sha else ""
    state = (f"merged to `{where}`{at}, not released" if where else f"unmerged{at}")
    if not summarised:
        return (f"{state}; Ledger could not read its diff, so only its existence is known — "
                "its branch name and commit titles are not evidence of what it does")
    against = f" against `{base}`" if base else ""
    return (f"{state}; summarised by Ledger from its diff{against} (names and directions, "
            "never code). You have not read it: a branch name or commit title is not "
            "evidence — trace the mechanism before citing it")


def ask_line(ref: str, author: str | None, shared: Shared | None, fields: list[str]) -> str:
    """One question for the author or the agent, aimed at the mechanism.

    Not "is X in the set", which a diff answers trivially and wrongly: the
    question is what crosses the boundary and whether it still means the same
    thing on the other side.
    """
    who = f" Ask {author}." if author else ""
    if shared is None:
        return (f"What does `{ref}` change that this change reads, calls or depends on, and "
                "which of the two must land first?" + who)
    name = f"`{shared.contract.name}`"
    named = ", ".join(f"`{f}`" for f in fields[:2])
    if shared.removed:
        return (f"`{ref}` removes its side of {name}: does this change still depend on it, "
                "and which lands first?" + who)
    if shared.theirs == Edge.PUBLISHES and shared.yours == Edge.CONSUMES:
        if fields:
            return (f"Does `{ref}` change the presence, format or meaning of {named} that this "
                    f"change reads from {name}?" + who)
        return (f"Which fields of {name} does `{ref}` add, drop or reformat, and when does it "
                "emit — does this change rely on any of that?" + who)
    if shared.theirs == Edge.CONSUMES and shared.yours == Edge.PUBLISHES:
        if fields:
            return (f"Does `{ref}` require {named} on {name} in a form this change does not "
                    "send?" + who)
        return (f"What does `{ref}` expect to receive on {name} — does this change send it, "
                "in that form, and deploy before it?" + who)
    return (f"Both changes touch {name}: are they compatible, and which must land first?" + who)
