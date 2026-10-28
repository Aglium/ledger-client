"""The impact brief: what this change touches, who it affects, and what to do.

This is the deliverable. Everything upstream -- the ticket join, the contract
cards, the decision layer -- exists to produce it.

It ends in a **recommendation**, not a pile of evidence, because the consumers
are a coding-agent skill and a CI gate, and neither can act on "here are some
facts, you decide". The evidence is still carried alongside, so a consumer can
disagree with the verdict and show its work.

The one rule that matters most:

    INSUFFICIENT_DATA is never PROCEED.

An incomplete index that recommends proceeding is worse than no tool at all: it
converts "we do not know" into "it is fine" at exactly the moment someone is
deciding whether to merge. Every path that cannot see enough says so.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum

from ledger.core.branches import Patterns
from ledger.core.contracts import ContractRef
from ledger.core.decision import Decision, DecisionSource, DecisionStatus
from ledger.core.join import Change, JoinResult


class Recommendation(str, Enum):
    PROCEED = "proceed"
    REVIEW_WITH = "review_with"
    DECISION_NEEDED = "decision_needed"
    INSUFFICIENT_DATA = "insufficient_data"

    @property
    def rank(self) -> int:
        return {"proceed": 0, "review_with": 1, "decision_needed": 2, "insufficient_data": 3}[self.value]


class LinkStrength(str, Enum):
    STRONG = "strong"      # deterministic: same ticket, or the decision names this file
    MEDIUM = "medium"      # reserved; no rule currently produces it
    WEAK = "weak"          # same repo, overlapping subject -- context, never escalates


@dataclass
class DecisionLink:
    decision_id: str
    title: str
    repo: str
    path: str
    status: str
    deciders: list[str]
    strength: str
    why: str
    open_gates: list[str] = field(default_factory=list)
    gate_hit: str | None = None
    #: Why the gate is live for this change, so a consumer can check the
    #: verdict's reasoning without re-deriving it.
    gate_basis: str | None = None
    #: Set when the document itself is in flight: the branch it is being
    #: written on. A decision still being drafted is not settled law, and
    #: whoever is drafting it is the person to talk to.
    ref: str | None = None
    #: True when the linked copy sits on an integration branch: decided, not yet
    #: released -- context, never a reason to stop.
    integrated: bool = False
    #: What produced the link: "contract", "path", "gate", "ticket" or "words".
    #: Only the first three are evidence that the decision is about what this
    #: change does. A ticket is an administrative container -- one real ticket
    #: held ten unrelated items -- so it links, and never escalates.
    basis: str = "ticket"


@dataclass
class ContractImpact:
    """A contract this change touches, and who else touches it.

    This is what makes an impact claim defensible. "This change touches
    `order_line`, and the billing service writes it too" survives an argument.
    "These two diffs share some tokens" does not.
    """

    surface: str
    name: str
    also_touched_by: list[str] = field(default_factory=list)
    #: Other repos' part in the contract: [{repo, uses, how}]. `also_touched_by`
    #: counts only real producers and consumers -- a library that merely names
    #: a topic does not share it.
    parties: list[dict] = field(default_factory=list)
    #: What other repos do with each field of this contract, in words:
    #: [{repo, direction, field, role}]. A pointer tells an agent where to
    #: look; on an estate this size, nowhere has it all checked out, so the
    #: answer has to arrive derived.
    fields: list[dict] = field(default_factory=list)
    #: Documentation citing this contract: [{title, url, path, updated}].
    #: A change to a documented contract is a change that leaves a page wrong
    #: unless somebody updates it, and the page's own date says how far
    #: behind it already was.
    documented_in: list[dict] = field(default_factory=list)


@dataclass
class Impact:
    repo: str
    ref: str
    kind: str
    author: str | None
    shared_tickets: list[str]
    where: str | None = None        # integration branch, when merged-not-released
    #: The commit, for work that has already merged. Real feedback: "the branch
    #: name it gave wasn't in the local clone. The work was commit 9f95e28 on
    #: stage." Naming a branch that was deleted when it merged sends the reader
    #: looking for something that is not there; the commit is still there.
    sha: str | None = None
    #: Why this is believed to be related, when it is not a ticket in the
    #: branch name. Said out loud because these links are weaker and the
    #: reader has to weigh them: a ticket written in the diff body is nearly
    #: as good as one in the branch name, and "same author, minutes apart" is
    #: a question rather than a finding.
    how: str | None = None
    #: The next four make the item stand on its own for a reader who cannot
    #: open the other repository. Real feedback, after a review built on a
    #: bare pointer was checked by the branch's author: "The brief gave only a
    #: branch name and author... the model's conclusion from it was inverted."
    #: All four are derived from the two diffs (see `core.effect`) and carry
    #: names, directions and roles -- never the other repository's code.
    #:
    #: What its diff adds, removes and changes, in one line.
    effect: str | None = None
    #: Which way it faces this change, over which contract, and which field
    #: names cross between them.
    relation: str | None = None
    #: How much of this anybody has read: unmerged or merged-unreleased, at
    #: which commit, and that a title is not evidence.
    evidence: str | None = None
    #: One question for its author, aimed at the mechanism.
    ask: str | None = None
    #: What it does at runtime, as a model summarised its diff at sync time:
    #: {text, touches, confidence, partial, model, prompt_version}. The one
    #: statement here that is not derived deterministically, so it is carried
    #: apart from `effect`, labelled wherever it is shown, and is a lead to
    #: verify rather than a fact. None without a server, a model or a summary.
    behaviour: dict | None = None
    #: The contract both diffs name, when they share one: what `relation`
    #: rests on. None when the two are linked by something else.
    over: str | None = None
    #: When the two changes cannot both land as they are -- one removes what
    #: the other uses -- the sentence that says so. See `effect.opposite_moves`.
    conflict: str | None = None
    #: The two diffs share no name; only the other change's REPOSITORY touches
    #: a contract this one does. Not a link the work made, so it never leads.
    by_repository: bool = False
    #: Names both diffs use (see `core.effect.shared_names`): the content
    #: behind a link that a ticket key or a clock made. None when the other
    #: diff was not read, [] when it was and they share nothing.
    shares: list[str] | None = None


@dataclass
class FieldBreak:
    """A field this change removes that another repo depends on."""

    surface: str
    name: str
    field_path: str
    #: Repos that read or require the field, with how they use it.
    readers: list[dict]
    evidence: str
    path: str


@dataclass
class ContractBreak:
    """A contract this change stops naming, that another repo uses."""

    surface: str
    name: str
    also_touched_by: list[str]
    evidence: str
    path: str


@dataclass
class ImpactBrief:
    repo: str
    ref: str
    tickets: list[str]
    recommendation: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    impacts: list[Impact] = field(default_factory=list)
    decisions: list[DecisionLink] = field(default_factory=list)
    contracts: list[ContractImpact] = field(default_factory=list)
    #: Removed fields someone else depends on. The canonical break this whole
    #: product exists to catch, and the only evidence that is close to
    #: deterministic: a deleted line, a named reader, a role.
    field_breaks: list[FieldBreak] = field(default_factory=list)
    #: Whole contracts this change deletes that others use -- the route, topic
    #: or table itself, rather than one of its fields.
    contract_breaks: list[ContractBreak] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    #: The subset of `reasons` that nothing in this repository can show: work
    #: merged to an integration branch, or sitting on another repo's branch.
    #: Carried separately because renderers kept presenting it as one bullet
    #: among several and agents kept skipping it. A measured case: a brief said
    #: `order_refund_row` was on `stage`, unreleased; the session that got
    #: it rediscovered the two locally-findable facts in the same brief by
    #: hand and never mentioned this one. These strings are also in `reasons`,
    #: so a renderer that ignores this field loses nothing.
    not_local: list[str] = field(default_factory=list)
    index_complete: bool = True
    missing_sources: list[str] = field(default_factory=list)
    #: The branch this change was diffed against, and how many commits it has
    #: beyond it. Zero means the work is already integrated there.
    base: str | None = None
    own_commits: int | None = None
    resolved: bool = True
    #: Files changed in the working tree and not committed, when the brief was
    #: asked to read them. An agent's work lives here for most of a session.
    uncommitted: list[str] = field(default_factory=list)
    #: Where the estate-wide facts came from. "stored" is the synced index,
    #: "live" a full read for this call, "mixed" stored plus the repos in
    #: `live_repos`, which moved since the last sync and were read live.
    index_source: str = "live"
    #: When the in-flight state this answer used was last synced, ISO. None
    #: when it was read live. In-flight work is the one thing a stored answer
    #: cannot re-verify per call, so an answer that does not carry its age
    #: cannot be told apart from a fresh one -- and a fifteen-hour-old
    #: "nothing in flight" reads exactly like a true one.
    synced_at: str | None = None
    live_repos: list[str] = field(default_factory=list)
    unreadable_repos: list[str] = field(default_factory=list)
    #: Which items lead and which are only mentioned (`core.rank`). Set after
    #: the brief is complete, because ranking reads the whole of it.
    ranked: dict | None = None
    #: What the diff changes, kept for ranking and never serialised: file
    #: paths, and the identifiers its added and removed lines name.
    changed_paths: list[str] = field(default_factory=list)
    diff_names: list[str] = field(default_factory=list)
    #: The verdict and confidence the links alone gave, before `settle`
    #: weighed them against the ranking. Never serialised.
    linked: tuple[str, str] | None = None
    #: The verdict this brief would carry with no in-flight items and no
    #: shared contracts: what `settle` falls back to when none of them is
    #: strong enough to lead. A decision that governs the change is not
    #: overruled because the items beside it were weak. Never serialised.
    quiet: tuple[str, str, list] | None = None

    def to_dict(self) -> dict:
        """Stable shape. A gate plugin depends on this, so it is explicit rather
        than whatever the dataclass happens to serialise to.

        Version 2 added `contracts`. Version 3 added `subject.resolved`,
        `subject.base`, `subject.own_commits` and `decisions[].gate_basis`, and
        changed what can produce decision_needed: shared words no longer can.
        Version 4 added `index.source`, `index.live_repos` and
        `index.unreadable_repos`. Version 5 added `decisions[].ref` -- the
        branch a decision is being drafted on, when it has not merged -- and
        made a strong link to such a document produce decision_needed.
        Version 6 added `decisions[].basis` and narrowed what can escalate:
        only a decision naming a contract or file this change touches, never
        one merely filed under the same ticket. Version 7 added
        `field_breaks`: fields this change removes that another repo reads.
        Version 8 added `contract_breaks`: whole contracts it stops naming.
        Version 9 added `contracts[].parties`: each other repo's role in a
        contract and how it is known (declared, via a binding, or literal).
        Version 10 added `contracts[].documented_in`: the documentation pages
        that cite the contract, with each page's own last-updated date.
        Version 11 added `subject.uncommitted`: files changed in the working
        tree and not committed, which are read as part of the change.
        Version 12 added `index.synced_at`: when the in-flight state behind
        this answer was last synced, so a stale "nothing in flight" can be
        told apart from a fresh one. Version 13 added `impacts[].sha`, because
        work that has merged is named after a branch that was deleted when it
        merged, and the commit is what a reader can still open.
        Version 15 added `impacts[].how` and `not_local`. `how` says why a
        change is believed related when no ticket in the branch name links
        them -- a ticket written in the diff body, or the same author pushing
        minutes apart -- because those links are weaker and a reader has to
        weigh them. `not_local` is the subset of `reasons` that nothing in
        the repository being briefed can show.
        Version 16 added `impacts[].effect`, `relation`, `evidence` and `ask`:
        what an in-flight change does, which way it faces this one and over
        which contract, how much of it has been read, and the question to put
        to its author -- so an item can be acted on without opening a
        repository the reader may not have. Version 17 added
        `impacts[].behaviour`: a model's one-line summary of what the change
        does at runtime, with its confidence and whether the diff was cut.
        Version 18 added `ranked`: the few items a reader should weigh first,
        each with a line saying how to check it, and the rest counted for one
        line. It also narrowed decision_needed from a decision: the contract
        it names must be one another repository produces or consumes.

        Version 19 added `impacts[].by_repository`: true when the only link is
        what the other change's repository does, not what its diff does.

        Version 20 added `impacts[].shares`: the names both diffs use, so a
        link made by a ticket key or the clock can say whether the work meets.
        It also stopped listing in-flight branches with no work of their own,
        decisions the change itself writes, and superseded decisions, and
        made `review_with` depend on an item strong enough to lead.

        Additive changes still bump the version, because a consumer is free to
        validate strictly, and finding out from a production gate that the
        payload grew a key is not acceptable.
        """
        return {
            "schema": 20,
            "subject": {
                "repo": self.repo, "ref": self.ref, "tickets": self.tickets,
                "resolved": self.resolved, "base": self.base, "own_commits": self.own_commits,
                "uncommitted": self.uncommitted,
            },
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "not_local": self.not_local,
            "actors": self.actors,
            "index": {
                "complete": self.index_complete, "missing_sources": self.missing_sources,
                "source": self.index_source, "live_repos": self.live_repos,
                "unreadable_repos": self.unreadable_repos, "synced_at": self.synced_at,
            },
            "contracts": [asdict(c) for c in self.contracts],
            "field_breaks": [asdict(f) for f in self.field_breaks],
            "contract_breaks": [asdict(c) for c in self.contract_breaks],
            "impacts": [asdict(i) for i in self.impacts],
            "decisions": [asdict(d) for d in self.decisions],
            "ranked": self.ranked,
        }


def _norm_rel(rel: str) -> str:
    """`../../app/services/x.py` -> `app/services/x.py`."""
    return re.sub(r"^(?:\.\./)+", "", (rel or "").strip()).lstrip("./")


#: More decisions of one repository than this naming one file make it a shared
#: page rather than the subject of any of them.
SHARED_FILE = 3


_PROSE_PATH = re.compile(r"\.(md|markdown|rst|txt|adoc)$|(^|/)(docs?|CHANGELOG)[/.]", re.I)


def _own_paths(d, repo: str) -> list[str]:
    """The record's related paths that are in `repo`: all of them when it is
    that repository's record, else only those spelled `repo/...`."""
    if d.repo == repo:
        return list(d.related or [])
    pre = repo + "/"
    return [n[len(pre):] for n in map(_norm_rel, d.related or []) if n.startswith(pre)]


def _related_hit(related: list[str], changed_paths: list[str]) -> str | None:
    """Match on a real path suffix, not a basename.

    Basename matching linked five unrelated ADRs to one change, because several
    point at the same common handler. A decision "names a file this change
    touches" only if the paths actually agree.
    """
    for rel in related:
        norm = _norm_rel(rel)
        if len(norm.split("/")) < 2:
            continue
        for p in changed_paths:
            if p == norm or p.endswith("/" + norm) or norm.endswith("/" + p):
                return norm
    return None


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text or "")}


_DISTINCTIVE = re.compile(r"[_./\-{]")


def _names_contract(text: str, contracts: list[ContractRef]) -> ContractRef | None:
    """Does this text name a contract the change touches?

    Deliberately literal: a decision either names `order_line` or it does not,
    and a gate that blocks a merge has to be able to point at the word.

    A distinctive name -- `order_line`, `order.events.v1`, `/orders/{id}` --
    matches on word boundaries. A plain word does NOT: a table called `invoice`
    or `incident` shares its name with ordinary prose, and matching it that way
    linked a rating change to an unrelated eligibility ADR because both used the
    word "invoice". A plain word only counts where the text marks it as an
    identifier -- in backticks or quotes, or as "the invoice table".
    """
    for c in contracts:
        name = c.name
        if len(name) < 4:
            continue
        esc = re.escape(name)
        if _DISTINCTIVE.search(name):
            if re.search(rf"(?<![\w.]){esc}(?![\w])", text or "", re.I):
                return c
            continue
        if re.search(rf"[`'\"]{esc}[`'\"]", text or "", re.I) \
                or re.search(rf"\btable\s+{esc}\b|\b{esc}\s+table\b", text or "", re.I):
            return c
    return None


#: Nobody to involve. A machine account appears as an author on merge and
#: release commits, and "involve acme-ci-bot[bot]" is not an action.
_BOT = re.compile(r"\[bot\]|(\A|[-_.])(bot|ci|cd|jenkins|renovate|dependabot|"
                  r"github-actions|gitlab-runner|automation|svc|service-account)"
                  r"([-_.@]|\Z)", re.I)


def _is_bot(who: str) -> bool:
    return bool(_BOT.search(who or ""))


def _roles(ci) -> str:
    """`consumed by billing, legacy; published by orders` -- or plain repo names.

    A party on the same *channel* under a different topic name is named
    separately and never folded into "published by". Leads publishes
    `acme-leads.intake-drafts`, not the orchestrator's own
    `acme-orchestrator-service.intake-drafts`, and saying it publishes the
    latter is false about the one thing the reader will go and check.
    """
    if not ci.parties:
        return "also in " + ", ".join(ci.also_touched_by)
    by: dict[str, list[str]] = {}
    elsewhere: list[str] = []
    for p in ci.parties:
        if p["uses"] not in ("publishes", "consumes"):
            continue
        if str(p.get("how") or "").startswith("same channel"):
            verb = "publishes" if p["uses"] == "publishes" else "consumes"
            elsewhere.append(f"{p['repo']} {verb} the same channel ({p['how'].split('declared as ')[-1]})")
            continue
        by.setdefault(p["uses"], []).append(p["repo"])
    parts = [f"{'published' if k == 'publishes' else 'consumed'} by {', '.join(v)}"
             for k, v in sorted(by.items())]
    parts += elsewhere
    return "; ".join(parts) or "also in " + ", ".join(ci.also_touched_by)


def _document_key(d: Decision) -> tuple:
    """Which decision a document IS, independent of its exact text.

    Keyed by file, not by content hash: on real data one merged ADR sat on main
    and, slightly edited, on five branches. Hashing content made those six
    different decisions, and the branch copies read as "not yet decided" --
    stopping a change the merged ADR already governed. A TODOS file holds many
    entries, so there the title is part of the identity.
    """
    return (d.repo, d.path, d.title if d.source == DecisionSource.TODO else "")


def _rank(d: Decision, integration: frozenset[str]) -> int:
    """Merged beats integrated beats in-draft: the most settled copy speaks."""
    if d.ref is None:
        return 0
    return 1 if d.ref in integration else 2


def _collapse_copies(decisions: list[Decision],
                     integration: frozenset[str] = frozenset()) -> tuple[list[Decision], dict[tuple, int]]:
    """One link per decision, not one per branch it sits on.

    The same ADR routinely rides several branches -- six copies of one
    platform ADR on six branches of one repo, on real data. Six identical
    links is noise that makes a brief look broken. A copy already on the
    default branch wins: it is settled, and nothing is waiting on it.

    Returns the kept decisions and, per (repo, decision), how many further
    copies were dropped. Returned rather than stashed: briefs run concurrently
    in the HTTP server, and shared state would mix two callers' counts.
    """
    copies: dict[tuple, int] = {}
    best: dict[tuple, Decision] = {}
    for d in decisions:
        key = _document_key(d)
        current = best.get(key)
        if current is None:
            best[key] = d
            continue
        copies[key] = copies.get(key, 0) + 1
        if _rank(d, integration) < _rank(current, integration) or (
                _rank(d, integration) == _rank(current, integration)
                and (d.ref or "") < (current.ref or "")):
            best[key] = d
    return list(best.values()), copies


def link_decisions(
    change: Change,
    decisions: list[Decision],
    *,
    changed_paths: list[str] | None = None,
    contracts: list[ContractRef] | None = None,
    integration_branches: tuple[str, ...] = (),
) -> list[DecisionLink]:
    """Relate a change to the decisions that govern what it touches.

    Deterministic rules only, and every link explainable in one sentence to
    someone who disputes it -- the bar for anything a gate acts on.

    Only a STRONG link can carry an open gate, and only a gate can escalate to
    decision_needed. A gate is live for this change in exactly two cases:
      - the gate itself names a contract this change touches, or
      - the decision explicitly governs this ticket, so its open conditions
        are open conditions of this work.
    Shared words never escalate. An earlier rule did, and on real data it put
    an eligibility ADR from one repo on a rating change in another because
    both said "invoice".
    """
    changed_paths = changed_paths or []
    contracts = contracts or []
    integration = Patterns(integration_branches)
    decisions, copies = _collapse_copies(decisions, integration)
    tickets = {t.upper() for t in change.tickets}
    subject_tokens = _tokens(change.ref) | _tokens(change.title or "")
    out: list[DecisionLink] = []

    # How many decisions of one repository name each file. A file most of a
    # repository's records point at (a runbook, an overview, the README) says
    # nothing about which of them a change to it bears on: six ADRs naming
    # `docs/runtime.md` all matched a one-line edit there.
    named_by: dict[tuple[str, str], int] = {}
    for d in decisions:
        for rel in {_norm_rel(r) for r in d.related}:
            named_by[(d.repo, rel)] = named_by.get((d.repo, rel), 0) + 1
    changed_set = set(changed_paths)

    for d in decisions:
        # The change's own record: written or edited by this very diff, or
        # drafted on this branch. It is the author's account of the change,
        # not something the change has to answer to.
        if d.repo == change.repo and (d.path in changed_set or (d.ref and d.ref == change.ref)):
            continue
        # Superseded and rejected records bind nothing. Their options are
        # history; the record that replaced one is what a change answers to.
        if d.status in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED):
            continue
        strength: LinkStrength | None = None
        why = ""
        basis = "ticket"

        # Content first. Ticket equality used to be checked first and masked
        # it: a decision that named the very table this change touches was
        # recorded as a mere ticket match, and then could not escalate.
        governs_ticket = bool(d.ticket and d.ticket.upper() in tickets)
        if (hit := _names_contract(f"{d.title}\n{d.body}", contracts)) is not None:
            strength, why = LinkStrength.STRONG, f"names `{hit.name}`, a {hit.surface.value} this change touches"
            basis = "contract"
        elif (rel := _related_hit(_own_paths(d, change.repo), changed_paths)) is not None:
            # A path is relative to its repository: another service's
            # `app/api/deps.py` is a different file. A record elsewhere (a
            # central ADR repository) links only by naming this repository.
            if named_by.get((d.repo, rel), 0) > SHARED_FILE:
                strength = LinkStrength.WEAK
                why = (f"names {rel}, which this change touches and "
                       f"{named_by[(d.repo, rel)]} records of this repository name")
            elif _PROSE_PATH.search(rel):
                # A record naming a document the change edits: the two talk
                # about the same thing, which says nothing about what it rules.
                strength = LinkStrength.WEAK
                why = f"names {rel}, a document this change edits"
            else:
                strength, why = LinkStrength.STRONG, f"names {rel}, which this change touches"
            basis = "path"
        elif governs_ticket:
            strength, why = LinkStrength.STRONG, f"governs ticket {d.ticket}"
            basis = "ticket"

        gate_hit: str | None = None
        gate_basis: str | None = None
        for gate in d.open_gates:
            named = _names_contract(gate, contracts)
            if named is not None:
                gate_hit, gate_basis = gate, f"the open gate names `{named.name}`"
                basis = "gate"
                if strength is None:
                    strength, why = LinkStrength.STRONG, f"an open gate names `{named.name}`, which this change touches"
                    basis = "gate"
                break
        if gate_hit is None and governs_ticket and d.open_gates:
            gate_hit, gate_basis = d.open_gates[0], "the decision governs this ticket"

        if strength is None and d.repo == change.repo and (subject_tokens & _tokens(d.title)):
            # Context only. A weak link is shown, never escalated.
            strength, why = LinkStrength.WEAK, "same repo, overlapping subject"
            basis = "words"

        if strength is None:
            continue

        extra = copies.get(_document_key(d), 0)
        if d.ref and d.ref in integration:
            # Decided and integrated, waiting for release: not a decision still
            # to be made. On real data an accepted ADR on `stage` stopped a
            # branch as if it were still being drafted.
            why = f"{why}; on `{d.ref}`, not yet on the default branch"
        elif d.ref:
            why = f"{why}; on `{d.ref}`, not merged"
            if extra:
                why += f" (also on {extra} other branch(es))"
        elif extra:
            # Merged, and being edited elsewhere. Worth knowing; not a decision
            # still to be made.
            why = f"{why}; being amended on {extra} branch(es)"

        out.append(DecisionLink(
            decision_id=d.global_id, title=d.title, repo=d.repo, path=d.path,
            status=d.status.value, deciders=list(d.deciders),
            strength=strength.value, why=why,
            open_gates=list(d.open_gates[:3]), gate_hit=gate_hit, gate_basis=gate_basis,
            ref=d.ref, basis=basis, integrated=bool(d.ref and d.ref in integration),
        ))

    order = {"strong": 0, "medium": 1, "weak": 2}
    out.sort(key=lambda x: (order[x.strength], x.repo, x.title))
    return out


def build_brief(
    siblings: JoinResult,
    decisions: list[Decision],
    *,
    changed_paths: list[str] | None = None,
    contracts: list[ContractRef] | None = None,
    contract_owners: dict[ContractRef, list[str]] | None = None,
    field_breaks: list[FieldBreak] | None = None,
    contract_breaks: list[ContractBreak] | None = None,
    contract_parties: dict | None = None,
    contract_docs: dict | None = None,
    #: {contract: [{repo, direction, field, role}]} for OTHER repositories.
    contract_fields: dict | None = None,
    #: Contracts this change touches that are ALSO touched on an integration
    #: branch of the same repository: {contract: [branch]}.
    same_repo_integration: dict | None = None,
    integration_branches: tuple[str, ...] = (),
    resolved: bool = True,
    base: str | None = None,
    own_commits: int | None = None,
    uncommitted: list[str] | None = None,
    integrated_in: str | None = None,
) -> ImpactBrief:
    """Assemble the brief and commit to a recommendation.

    `resolved=False` means the change could not be found in any source. That
    is the case an earlier version answered with PROCEED at high confidence --
    "we have never heard of this" came out as "it is fine".
    """
    subject = siblings.subject
    sibling_repos = {s.change.repo for s in siblings.siblings}
    # "No commits beyond the base" means the work is already integrated and
    # touches nothing of its own -- but only when there is no uncommitted work
    # either. Without that second half, a session that has edited nine files
    # and committed none is told it touches nothing.
    empty = own_commits == 0 and not uncommitted
    contracts = [] if empty else (contracts or [])
    contract_owners = contract_owners or {}
    field_breaks = [] if empty else (field_breaks or [])
    contract_breaks = [] if empty else (contract_breaks or [])
    links = link_decisions(subject, decisions, changed_paths=changed_paths, contracts=contracts, integration_branches=integration_branches)

    brief = ImpactBrief(
        repo=subject.repo, ref=subject.ref, tickets=list(subject.tickets),
        recommendation=Recommendation.PROCEED.value, confidence="high",
        index_complete=not siblings.partial,
        missing_sources=list(siblings.missing_sources),
        base=base, own_commits=own_commits, resolved=resolved,
        uncommitted=list(uncommitted or []),
    )
    brief.field_breaks = list(field_breaks)
    brief.contract_breaks = list(contract_breaks)
    brief.contracts = [
        ContractImpact(
            surface=c.surface.value, name=c.name,
            also_touched_by=sorted(r for r in contract_owners.get(c, []) if r != subject.repo),
            parties=[{"repo": p["repo"], "uses": p["uses"], "how": p.get("how", "literal")}
                     for p in (contract_parties or {}).get(c, []) if p["repo"] != subject.repo],
            documented_in=(contract_docs or {}).get(c, []),
            fields=(contract_fields or {}).get(c, [])[:12],
        )
        for c in contracts
    ]
    brief.impacts = [
        Impact(repo=s.change.repo, ref=s.change.ref, kind=s.change.kind,
               author=s.change.author, shared_tickets=list(s.shared_tickets),
               where=s.change.integration_ref,
               sha=(s.change.sha or "")[:12] or None)
        for s in siblings.siblings
    ]

    # Gates that this change actually hits, on decisions that still bind.
    #: Links that are about what this change does, not merely filed under the
    #: same ticket. Measured on real data: an umbrella ticket carrying ten
    #: unrelated items made ticket-only escalation a false alarm every time.
    ABOUT_THIS_CHANGE = ("contract", "path", "gate")
    #: Contracts another repository produces or consumes, as opposed to merely
    #: naming. A decision escalates only over one of these: on a week of real
    #: briefs, all ten decision_needed verdicts came from decisions naming a
    #: table this change touches -- `user`, in one -- and in three of the ten
    #: nothing in the brief bore on the change. A decision about a contract
    #: with a producer and a consumer, being changed on both sides (by this
    #: diff and by an open gate or a draft), is a decision somebody has to
    #: make; one that names a table everybody names is context.
    directed = {ci.name for ci in brief.contracts
                if any(p["uses"] in ("publishes", "consumes") for p in ci.parties)
                or any(f.get("direction") for f in ci.fields)}

    def over_directed(l: DecisionLink) -> bool:
        if l.basis not in ("contract", "gate"):
            return False
        named = re.findall(r"`([^`]+)`", f"{l.gate_basis or ''} {l.why}")
        return any(n in directed for n in named)

    hit_gates = [l for l in links
                 if l.gate_hit and l.basis in ABOUT_THIS_CHANGE
                 and l.status in (DecisionStatus.ACCEPTED.value, DecisionStatus.PROPOSED.value)
                 and over_directed(l)]
    strong = [l for l in links if l.strength == "strong"]
    #: Strong AND about what this change does. A ticket-only link is strong
    #: evidence of a relationship and no evidence of relevance.
    strong_about = [l for l in strong if l.basis in ABOUT_THIS_CHANGE]
    #: Of those, the ones that rule on what this change does to someone: a
    #: path in it, or a contract another repository produces or consumes.
    #: A record naming a table only this repository uses is context, as the
    #: ranking already treats it; on a real week it was noise every time.
    governing = [l for l in strong_about if l.basis == "path" or over_directed(l)]
    # A decision still unmerged, excluding one on the subject's own branch: a
    # change carrying its own ADR is not blocked on it. It must also be about
    # what this change does -- see ABOUT_THIS_CHANGE.
    drafting = [l for l in strong
                if l.ref and not l.integrated and l.ref != subject.ref
                and l.basis in ABOUT_THIS_CHANGE and over_directed(l)]
    #: Linked, named in the brief, but never a reason to stop.
    context_only = [l for l in strong if l.basis not in ABOUT_THIS_CHANGE]
    #: Strong and about this change, but over a contract no other repository
    #: produces or consumes: worth reading, not a reason to stop.
    nearby = [l for l in strong_about if l not in governing]

    governs = (f"a binding decision governs this work: {governing[0].title[:90]}"
               if governing else None)
    filed_with = None
    if nearby:
        l = nearby[0]
        filed_with = (f"a decision {l.why.split(';')[0]}; no other repository produces or "
                      f"consumes it, so it is context: {l.title[:90]}")
    elif context_only:
        l = context_only[0]
        filed_with = (f"filed under the same ticket as a decision, which may or may not be about "
                      f"this change: {l.title[:90]}" + (f" (unmerged, on `{l.ref}`)" if l.ref else ""))
    brief.quiet = ((Recommendation.REVIEW_WITH.value, "medium", [governs]) if governs
                   else (Recommendation.REVIEW_WITH.value, "low", [filed_with]) if filed_with
                   else (Recommendation.PROCEED.value, "medium", []))

    if not resolved:
        # First of all. Nothing is known about this change, so nothing can be
        # recommended about it -- least of all that it is safe.
        brief.recommendation = Recommendation.INSUFFICIENT_DATA.value
        brief.confidence = "low"
        brief.reasons.append(
            "this change could not be found in any source, so nothing can be said about it"
        )
    elif siblings.partial:
        # Deliberately early. An incomplete index must never resolve to PROCEED,
        # whatever else it found.
        brief.recommendation = Recommendation.INSUFFICIENT_DATA.value
        brief.confidence = "low"
        brief.reasons.append(
            f"the index is incomplete ({', '.join(siblings.missing_sources) or 'unknown source'} "
            "unreachable), so absence of impact is not evidence of absence"
        )
    elif field_breaks or contract_breaks:
        # Ranked above every other finding: a named reader loses a field it
        # reads. This is evidence, not inference -- a deleted line, a repo that
        # depends on it, and what it uses the field for.
        brief.recommendation = Recommendation.DECISION_NEEDED.value
        brief.confidence = "high"
        for c in contract_breaks[:3]:
            brief.reasons.append(
                f"stops naming {c.surface} `{c.name}` in {c.path}, which "
                f"{', '.join(c.also_touched_by)} also touch(es)"
            )
        for b in field_breaks[:4]:
            who = ", ".join(f"{r['repo']} ({r['direction']})" for r in b.readers[:4])
            brief.reasons.append(
                f"removes `{b.field_path}` from {b.surface} `{b.name}`, which {who} depend(s) on"
                + (f" — {b.readers[0]['role']}" if b.readers and b.readers[0].get("role") else "")
            )
    elif hit_gates:
        brief.recommendation = Recommendation.DECISION_NEEDED.value
        # High only when the gate names a contract this change touches. A gate
        # that is live merely because the decision governs the ticket may be
        # about a different part of that ticket.
        brief.confidence = "high" if any((l.gate_basis or "").startswith("the open gate names")
                                         for l in hit_gates) else "medium"
        for l in hit_gates[:3]:
            brief.reasons.append(
                f"{l.title[:80]} ({l.status}) has an unresolved condition that applies here "
                f"({l.gate_basis}): \"{(l.gate_hit or '')[:140]}\""
            )
    elif drafting:
        # The decision governing this work is being written right now, on a
        # branch, by someone. That is the definition of a decision that has not
        # been made -- and it is invisible to anyone reading default branches.
        brief.recommendation = Recommendation.DECISION_NEEDED.value
        brief.confidence = "medium"
        for l in drafting[:3]:
            who = f", by {', '.join(l.deciders)}" if l.deciders else ""
            brief.reasons.append(
                f"a decision governing this work sits on `{l.ref}`{who}, unmerged: "
                f"{l.title[:90]} ({l.status}); it {l.why.split(';')[0]}"
            )
    elif brief.impacts or any(ci.also_touched_by for ci in brief.contracts):
        brief.recommendation = Recommendation.REVIEW_WITH.value
        brief.confidence = "high"
        shared_ct = [ci for ci in brief.contracts if ci.also_touched_by]
        if shared_ct:
            top = shared_ct[:3]
            brief.reasons.append(
                "touches "
                + ", ".join(f"`{ci.name}` ({_roles(ci)})" for ci in top)
                + (f" and {len(shared_ct) - len(top)} more shared contract(s)" if len(shared_ct) > len(top) else "")
            )
        repos = sorted(sibling_repos)
        integrated = sum(1 for i in brief.impacts if i.kind == "integration")
        if brief.impacts:
            brief.reasons.append(
                f"{len(brief.impacts)} change(s) in {len(repos)} other repo(s) share this ticket: "
                + ", ".join(repos)
            )
        if integrated:
            brief.reasons.append(
                (f"{integrated} of them are merged" if integrated > 1 else "1 of them is merged")
                + " to an integration branch but not released, so "
                + ("they" if integrated > 1 else "it") + " will land before or alongside this"
            )
    elif governing:
        brief.recommendation = Recommendation.REVIEW_WITH.value
        brief.confidence = "medium"
        brief.reasons.append(governs)
    elif filed_with:
        brief.recommendation = Recommendation.REVIEW_WITH.value
        brief.confidence = "low"
        brief.reasons.append(filed_with)
    else:
        brief.reasons.append("no cross-repo work shares this ticket and no binding decision names it")

    ticket_gates = [l for l in links if l.gate_hit and l not in hit_gates and l.ref is None]
    if ticket_gates and brief.recommendation != Recommendation.DECISION_NEEDED.value:
        l = ticket_gates[0]
        brief.reasons.append(
            f"a decision filed under this ticket has an unresolved condition, which may not be "
            f"about this change: \"{(l.gate_hit or '')[:120]}\""
        )

    # A draft or an open gate over a contract nobody else produces or
    # consumes: still worth a line, never a stop.
    undirected = [l for l in strong
                  if l.basis in ABOUT_THIS_CHANGE and not over_directed(l)
                  and ((l.ref and not l.integrated and l.ref != subject.ref) or l.gate_hit)]
    if undirected and brief.recommendation != Recommendation.DECISION_NEEDED.value:
        l = undirected[0]
        where = f"is being drafted on `{l.ref}`" if l.ref else "has an open condition"
        brief.reasons.append(
            f"a decision that {l.why.split(';')[0]} {where} ({l.title[:80]}); no other repository "
            "produces or consumes what it names, so it is context, not a decision for this change")

    # In flight in this repository. Not in any PR list -- it has already
    # merged -- and invisible to a branch cut from the default branch.
    for c, refs in sorted((same_repo_integration or {}).items(),
                          key=lambda kv: (kv[0].surface.value, kv[0].name))[:3]:
        why = (f"{c.surface.value} `{c.name}` is also touched on `{', '.join(refs)}` in this "
               "repository: merged there, not released, and not on this branch")
        brief.reasons.append(why)
        brief.not_local.append(why)

    # Documentation that this change leaves wrong. Never a verdict of its own:
    # a documented contract is the normal case, and the failure being recorded
    # is that nothing updates the page -- so it is said where the change is
    # already worth reading, and attached to every contract either way.
    broken = ({(b.surface, b.name) for b in brief.contract_breaks}
              | {(f.surface, f.name) for f in brief.field_breaks})
    # Every documented contract this change touches, the ones it breaks or
    # shares first. Not only the shared ones: a table one service owns alone
    # still has a page describing it, and that page is the thing nobody
    # remembers to update.
    documented = sorted((ci for ci in brief.contracts if ci.documented_in),
                        key=lambda ci: (ci.surface, ci.name) not in broken and not ci.also_touched_by)
    for ci in documented[:3]:
        page = ci.documented_in[0]
        when = f", last updated {page['updated'][:10]}" if page.get("updated") else ""
        where = f" ({page['url']})" if page.get("url") else f" ({page['path']})"
        if page.get("behind"):
            n = page["behind"]
            latest = (f", latest \"{page['behind_subject'][:60]}\" "
                      f"{(page.get('behind_since') or '')[:10]}" if page.get("behind_subject") else "")
            tail = (f": {n} commit(s) have changed lines naming it since{latest}. "
                    "The page is behind the code this change adds to")
        else:
            tail = ": that page describes what this change alters, and nothing else will update it"
        brief.reasons.append(
            f"{ci.surface} `{ci.name}` is documented in \"{page['title'][:70]}\"{when}{where}{tail}"
        )

    if integrated_in and base:
        brief.reasons.append(
            f"its work is already merged into `{integrated_in}` and not yet into `{base}`, so it "
            f"is read against `{base}`, as a pull request to `{base}` would be; pass the target "
            "branch as `base` to read it against another"
        )
    if own_commits is None and base:
        brief.reasons.append(
            f"this branch does not exist yet; the rest is what `{base}` and the other repos "
            "already carry for its ticket"
        )
    if brief.uncommitted:
        brief.reasons.append(
            f"{len(brief.uncommitted)} file(s) are changed in the working tree and not "
            "committed; this reads them as part of the change"
        )
    elif resolved and own_commits == 0 and base:
        brief.reasons.append(
            f"it has no commits beyond `{base}`: its work is already integrated there, "
            "so it touches no contracts of its own"
        )

    brief.decisions = links[:6]
    actors: list[str] = []
    for l in links:
        actors.extend(l.deciders)
    actors.extend(i.author for i in brief.impacts if i.author)
    seen: dict[str, None] = {}
    for a in actors:
        if a and not _is_bot(a):
            seen.setdefault(a, None)
    # Six. "Involve these people" is an instruction, and an instruction naming
    # ten is none: a real brief on a cross-repo ticket listed ten including a
    # CI bot. Where a ticket genuinely spans that many, the reason line above
    # already says how many repositories and changes it is.
    brief.actors = list(seen)[:6]
    return brief


#: What `settle` says when it turns a linked-but-weak brief into `proceed`.
NOTHING_LEADS = ("nothing linked to this change is strong enough to check first: "
                 "the {n} item(s) below share only a ticket key, a file or a word with it")


def settle(b: ImpactBrief) -> ImpactBrief:
    """Let `review_with` stand only when an item is strong enough to lead.

    The verdict used to be `review_with` (high) whenever anything at all was
    linked, so the ranking could hide noise but never say "nothing here". On
    a real week that verdict was right 36% of the time; most of the rest were
    briefs whose every item shared an epic key, a common file or a word.

    Confidence follows the strongest item that leads: a link both diffs make
    (relation, same author pushing together) is high, a ticket key backed by
    shared names or a directed decision medium, a model's promotion low.

    Safe to call again after a re-rank (the server's, with a model): it
    starts from the verdict the links gave, kept on the brief.
    """
    if b.linked is None:
        b.linked = (b.recommendation, b.confidence)
    rec, conf = b.linked
    quiet_rec, quiet_conf, quiet_why = b.quiet or (Recommendation.PROCEED.value, "medium", [])
    note_prefix = NOTHING_LEADS.split("{n}")[0]
    if any(r.startswith(note_prefix) for r in b.reasons):      # settled before: undo it
        b.reasons = [r for r in b.reasons if not r.startswith(note_prefix) and r not in quiet_why]
    b.recommendation, b.confidence = rec, conf
    if rec != Recommendation.REVIEW_WITH.value:
        return b
    ranked = b.ranked or {}
    lead = ranked.get("lead") or []
    if lead:
        top = max(int(x.get("strength") or 0) for x in lead)
        by_lead = "high" if top >= 5 else "medium" if top >= 3 else "low"
        # Never above what the links themselves claimed: "same author, minutes
        # apart" was found as a question and stays one.
        order = ("low", "medium", "high")
        b.confidence = min(by_lead, conf, key=lambda c: order.index(c) if c in order else 2)
        return b
    if ranked.get("repeated"):
        return b                     # shown before on this branch, still standing
    # What the brief says without the weak items: proceed, or the decision
    # that governs it, which the items beside it never outranked.
    n = int((ranked.get("also") or {}).get("count") or 0)
    b.recommendation, b.confidence = quiet_rec, quiet_conf
    b.reasons.insert(0, NOTHING_LEADS.format(n=n))
    b.reasons.extend(r for r in quiet_why if r not in b.reasons)
    return b

