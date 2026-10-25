"""Which items of a brief lead, and which are only mentioned.

A brief used to list everything it found, and a coding agent read all of it.
Measured over one week of a real estate (227 distinct items shown at the start
of 24 sessions, each labelled by what the agent then did with it): 9% of the
items were useful, 57% were noise, and on decision_needed briefs 7% were
useful. Noise was not harmless: in a replay, the briefs that led with
unrelated tables and decisions got agents to build conclusions on them.

What predicted a useful item, on that week:

    same author, pushed around the same time        5 of 5 useful
    a ticket key this diff names                     5 of 9
    another repo's unreleased integration commit     3 of 32
    a machine account's branch                       1 of 17
    a decision                                       3 of 62
    a shared table with no direction                 0 of 20
    a documentation page behind the code             0 of 8

So a brief leads with a few items that have a reason to be there -- a link
from the work itself, not from an administrative container -- and says the
rest in one line. Nothing is hidden: the full lists stay in the payload and
behind the `brief` tool. What changes is what is put in front of a reader
who will act on it.

The one regression the replay found is a rule here too. A single weak item,
shown alone, reads as *the* finding: an agent given one "shares this ticket"
line and nothing else sent people to chase it, where the same agent had
dismissed it inside the long list. So weak items are never promoted to fill
an empty list. With nothing strong, the brief says nothing this diff touches.

Pure functions over the brief's own dictionary, so every renderer -- hook,
MCP, CLI -- shows the same choice, and a replay can score it offline.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

#: Items in the lead list. Three is what a reader still weighs one by one.
CAP = 3

#: A model's "does this touch your diff?" score at or above which a weak item
#: is promoted. 0-3: 3 names something this change adds, removes or calls;
#: 2 plausibly affects what it relies on. Measured: 2 kept 17 of 20 useful
#: items, 3 kept 5.
TOUCHES_PROMOTE = 2

_BOT = re.compile(r"\[bot\]|(\A|[-_.])(bot|ci|cd|jenkins|renovate|dependabot|"
                  r"github-actions|gitlab-runner|automation|svc|service-account)"
                  r"([-_.@]|\Z)", re.I)

#: How `core.effect.relation_line` opens when two diffs share no name.
_NO_SHARED_NAME = "Ledger found no topic, route or table named by both diffs"
#: How the relation opens when only the other change's REPOSITORY touches the
#: contract, for payloads before schema 19 that lack `by_repository`. Not a
#: link the work made: on a real estate four branches of one repository led a
#: brief only because that repository publishes a topic the change uses.
_BY_REPOSITORY = "its repository "


@dataclass
class Item:
    """One thing a brief could put in front of a reader, reduced to what the
    ranking needs. Built from a brief (`items_of`) or, by a replay, from text
    a past session was shown."""

    key: str
    kind: str               # impact | decision | contract | local | doc
    label: str              # how it is named in one line, names only
    #: Why it is linked: "relation" (both diffs name a contract), "author"
    #: (same author, pushed together), "diff_ticket" (a ticket key this diff
    #: names), "ticket" (a ticket key in both branch names), "integration"
    #: (an unreleased commit on an integration branch), "words", or "".
    link: str = ""
    author: str | None = None
    #: The contract both sides name, when there is one.
    names: list[str] = field(default_factory=list)
    #: True when another repository produces or consumes the contract this
    #: item is about, rather than merely naming it.
    directed: bool = False
    #: A model's 0-3 "does this touch your diff?", when one was asked.
    touches: int | None = None
    check: str = ""


@dataclass
class Ranked:
    lead: list[dict] = field(default_factory=list)
    #: Everything else, counted by kind and named by repository, for one line.
    also: dict = field(default_factory=dict)
    #: Items already shown for this branch, with when: {key: iso date}.
    repeated: dict = field(default_factory=dict)
    #: True when nothing is strong enough to lead.
    nothing: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def is_bot(who: str | None) -> bool:
    return bool(_BOT.search(who or ""))


def strength(it: Item) -> int:
    """0 means weak: mentioned in the one line, never led with.

    Strong links are ones the work itself made. A ticket is an administrative
    container -- one real ticket held ten unrelated items -- so a ticket in
    two branch names leads only on somebody's own branch, and an unreleased
    commit that merely carries the ticket does not lead at all.
    """
    if it.kind == "break":
        return 9
    if it.kind == "impact":
        if it.link == "relation":
            return 6
        if is_bot(it.author):
            return _promoted(it)
        if it.link == "author":
            return 5
        if it.link == "diff_ticket":
            return 4
        if it.link == "ticket":
            return 3
        return _promoted(it)
    if it.kind in ("decision", "contract") and it.directed:
        return 3
    return _promoted(it)


def _promoted(it: Item) -> int:
    return 2 if (it.touches or 0) >= TOUCHES_PROMOTE else 0


def check_line(it: Item, *, yours: list[str]) -> str:
    """How to confirm or dismiss an item quickly, in names and roles only.

    In a replay this line helped agents set noise aside: told where to look
    and what for, they said "does not apply" instead of guessing.
    """
    look = ", ".join(f"`{n}`" for n in (it.names or yours)[:4])
    what = f" for {look}" if look else ""
    if it.kind == "impact":
        return (f"how to check: read {it.label}{what}; if none of it appears there, "
                "say it does not apply")
    if it.kind == "decision":
        return f"how to check: read {it.label}; does its open condition hold for{what or ' this change'}?"
    if it.kind == "contract":
        return (f"how to check: find where the other side reads or writes {it.label}; "
                "does this change alter what they use?")
    return f"how to check: find {it.label} in this diff's changed lines; if it is not there, it does not apply"


def rank(items: list[Item], *, yours: list[str] | None = None,
         seen: dict[str, str] | None = None, cap: int = CAP) -> Ranked:
    """Lead with the strongest few; say the rest in one line.

    `seen` is what earlier answers already showed for this branch, {key: when}.
    A repeat is named in one line rather than shown again in full -- one
    session on a real estate was handed the same brief three times.
    """
    seen = seen or {}
    out = Ranked()
    scored = sorted(((strength(i), n, i) for n, i in enumerate(items)), key=lambda t: (-t[0], t[1]))
    lead: list[Item] = []
    rest: list[Item] = []
    for s, _, it in scored:
        if s and it.key in seen:
            out.repeated[it.key] = seen[it.key]
            continue
        (lead if s and len(lead) < cap else rest).append(it)
    for it in lead:
        it.check = it.check or check_line(it, yours=yours or [])
        out.lead.append({"key": it.key, "kind": it.kind, "label": it.label, "link": it.link,
                         "strength": strength(it), "check": it.check})
    out.nothing = not lead and not out.repeated
    by_kind: dict[str, int] = {}
    repos: dict[str, int] = {}
    for it in rest:
        by_kind[it.kind] = by_kind.get(it.kind, 0) + 1
        repo = it.key.split(":")[1] if it.kind == "impact" and it.key.count(":") >= 2 else None
        if repo:
            repos[repo] = repos.get(repo, 0) + 1
    out.also = {"count": len(rest), "by_kind": by_kind, "repos": repos,
                "keys": [it.key for it in rest]}
    return out


# --------------------------------------------------------- from a brief --

def impact_key(i: dict) -> str:
    return f"impact:{i['repo']}:{i.get('sha') or i['ref']}"


def decision_key(d: dict) -> str:
    return f"decision:{d['repo']}/{d['path']}"


def contract_key(c: dict) -> str:
    return f"contract:{c['surface']}:{c['name']}"


def local_key(line: str) -> str:
    return "local:" + re.sub(r"\s+", " ", line)[:80]


def _names_in(text: str) -> list[str]:
    return re.findall(r"`([^`]{3,80})`", text or "")


def items_of(b: dict, *, touches: dict[str, int] | None = None) -> list[Item]:
    """The candidate items of a brief payload (`ImpactBrief.to_dict()`)."""
    touches = touches or {}
    out: list[Item] = []
    contracts = {c["name"]: c for c in b.get("contracts", [])}

    def directed(name: str) -> bool:
        c = contracts.get(name)
        if not c:
            return False
        return any(p.get("uses") in ("publishes", "consumes") for p in c.get("parties", [])) \
            or any(f.get("direction") for f in c.get("fields", []))

    for f in b.get("field_breaks", []):
        out.append(Item(key=f"break:{f['surface']}:{f['name']}:{f['field_path']}", kind="break",
                        label=f"{f['surface']} `{f['name']}` field `{f['field_path']}`",
                        names=[f["name"]], directed=True))
    for c in b.get("contract_breaks", []):
        out.append(Item(key=f"break:{c['surface']}:{c['name']}", kind="break",
                        label=f"{c['surface']} `{c['name']}`", names=[c["name"]], directed=True))
    for i in b.get("impacts", []):
        how, rel = i.get("how") or "", i.get("relation") or ""
        by_repo = i.get("by_repository")
        if by_repo is None:                 # an older payload: read the sentence
            by_repo = rel.startswith(_BY_REPOSITORY)
        if rel and not by_repo and not rel.startswith(_NO_SHARED_NAME):
            link = "relation"
        elif how.startswith("same author"):
            link = "author"
        elif "which this diff names" in how:
            link = "diff_ticket"
        elif i.get("kind") == "integration" or i.get("where"):
            link = "integration"
        elif i.get("shared_tickets"):
            link = "ticket"
        else:
            link = "words" if how else ""
        key = impact_key(i)
        label = (f"{i['repo']} `{i['sha']}` (on {i['where']})" if i.get("where") and i.get("sha")
                 else f"{i['repo']} branch `{i['ref']}`")
        out.append(Item(key=key, kind="impact", label=label, link=link, author=i.get("author"),
                        names=_names_in(rel) if link == "relation" else [],
                        touches=touches.get(key)))
    for d in b.get("decisions", []):
        key = decision_key(d)
        named = _names_in(d.get("gate_basis") or "") + _names_in(d.get("why") or "")
        out.append(Item(key=key, kind="decision", label=f"{d['repo']}/{d['path']}",
                        link=d.get("basis") or "", names=named[:2],
                        directed=d.get("basis") in ("contract", "gate") and any(directed(n) for n in named),
                        touches=touches.get(key)))
    for c in b.get("contracts", []):
        if not c.get("also_touched_by"):
            continue
        key = contract_key(c)
        out.append(Item(key=key, kind="contract", label=f"{c['surface']} `{c['name']}`",
                        names=[c["name"]], directed=directed(c["name"]), touches=touches.get(key)))
    for line in b.get("not_local", []):
        key = local_key(line)
        out.append(Item(key=key, kind="local", label=(_names_in(line) or [line[:40]])[0],
                        names=_names_in(line)[:1], touches=touches.get(key)))
    return out


def rank_brief(b: dict, *, touches: dict[str, int] | None = None,
               seen: dict[str, str] | None = None) -> dict:
    """`rank` over a brief payload, as the dictionary the payload carries."""
    yours = [c["name"] for c in b.get("contracts", [])][:4]
    return rank(items_of(b, touches=touches), yours=yours, seen=seen).to_dict()
