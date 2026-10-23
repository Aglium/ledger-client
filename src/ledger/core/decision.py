"""Layer 1: the decisions that govern a contract, and who is allowed to change them.

This is the half that turns "something else touches this" into "someone has to
decide something". A sibling list tells a reviewer where to look; a governing
decision tells them what was already settled, by whom, and whether the change in
front of them contradicts it.

ADRs with structured frontmatter -- `status`, `deciders`, `ticket`,
`related_design` -- make authority a lookup rather than an inference. Where that
exists this layer is cheap: no model is needed for the metadata, only for
relating a decision to the contracts it binds. Headings are the fallback.

Two sources, because both exist and neither is complete:
  - `docs/adr/**` -- deliberate, dated, with named deciders
  - `TODOS.md`    -- deferred work, often carrying cross-repo blockers that no
                     ADR records ("blocks Phase A.2 edit UI")
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

import yaml


#: Bump whenever a change here alters what the ADR/TODOS parsers return.
#: The stored index keys on it, as it does on contracts.EXTRACTOR_VERSION.
PARSER_VERSION = "1"


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    DEFERRED = "deferred"       # TODOS entries
    UNKNOWN = "unknown"


class DecisionSource(str, Enum):
    ADR = "adr"
    TODO = "todo"


@dataclass
class Decision:
    #: Stable across repos. A forked ADR copied into another repo collapses onto
    #: the same id, which is how `orchestrator-history-diff` stops looking like a
    #: second, independent decision.
    global_id: str
    source: DecisionSource
    repo: str
    path: str
    title: str
    status: DecisionStatus = DecisionStatus.UNKNOWN
    local_ref: str | None = None          # the per-repo number, e.g. "0008"
    deciders: list[str] = field(default_factory=list)
    ticket: str | None = None
    when: date | None = None
    related: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    #: Sentences that read as an unresolved condition. These are the live edges
    #: of a decision: an ADR marked "accepted" can still carry an open gate, and
    #: a change touching that gate is exactly when a human should be asked.
    open_gates: list[str] = field(default_factory=list)
    body: str = ""
    #: The branch this document was read from, when it is NOT the default one.
    #: A decision being written on a branch is a decision being made right now:
    #: it governs work in flight before it governs anything merged, and reading
    #: only default branches misses it entirely. `orcestrator_service/TODOS.md`
    #: is exactly this case -- tracked, cited, and never merged to main.
    ref: str | None = None

    @property
    def is_binding(self) -> bool:
        return self.status in (DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED)


_FM = re.compile(r"\A---\n(?P<fm>.*?)\n---\n(?P<body>.*)\Z", re.S)
_NUM = re.compile(r"(?:^|/)(\d{3,6})[-_]")

# Phrases that mark an unclosed condition inside an otherwise-settled decision.
_GATE_HINTS = (
    "remaining gate", "still lacks", "still missing", "not yet", "open question",
    "to be decided", "tbd", "blocks ", "blocked by", "follow-up", "out of scope",
    "deferred", "does not yet", "must still", "remains",
)


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def global_id_for(title: str, body: str) -> str:
    """Content-derived, so the same decision in two repos gets one id.

    Title alone is too fragile (a typo fix would fork it); body alone is too
    strict (a reconciliation note would). Title plus a body digest is the
    compromise that collapses genuine copies and keeps genuine edits separate.
    """
    digest = hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode()).hexdigest()[:10]
    return f"{_norm_title(title)[:60]}:{digest}"


def _gates(body: str, *, title: str = "") -> list[str]:
    """Sentences that state an unresolved condition.

    Precision matters more than recall here: a gate is what escalates a change
    to "someone must decide", and a gate quoted from a heading or a mid-list
    fragment cannot be argued with by the person being asked to decide. So the
    bar is a sentence that reads like a sentence.
    """
    out: list[str] = []
    norm_title = re.sub(r"\W+", "", title.lower())
    # Strip markdown emphasis and links BEFORE splitting. Otherwise a sentence
    # boundary inside `**bold.** More text` splits mid-emphasis and yields
    # fragments like "The split is deferred.** The" -- unquotable to the person
    # being asked to decide.
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", body, flags=re.S)
    clean = re.sub(r"`([^`]*)`", r"\1", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", clean)
    for raw in re.split(r"(?<=[.!?])\s+|\n", clean):
        line = raw.strip(" -*>#\t|")
        if not (40 < len(line) < 320):
            continue
        low = line.lower()
        if not any(h in low for h in _GATE_HINTS):
            continue
        if line.lstrip().startswith(("#", "|", "```", "ADR ")):
            continue
        # A heading restating the decision is not a gate.
        if norm_title and re.sub(r"\W+", "", low)[:60] in norm_title:
            continue
        if len(line.split()) < 7:
            continue
        # Mid-list fragments: a clause that opens on a separator or a bare
        # enumerator carries no standalone meaning.
        if re.match(r"^[;,)\]]|^\(", line.strip()):
            continue
        # A clause opening lowercase mid-thought is a fragment, not a statement.
        if line[:1].islower() and not line.lower().startswith(("the ", "a ", "an ", "this ", "it ")):
            continue
        out.append(re.sub(r"\s+", " ", line))
    seen: dict[str, None] = {}
    for g in out:
        seen.setdefault(g, None)
    return list(seen)[:8]


def parse_adr(repo: str, path: str, text: str) -> Decision | None:
    """Parse one ADR. Frontmatter when present; headings when not."""
    if not text.strip():
        return None
    m = _FM.match(text)
    meta: dict = {}
    body = text
    if m:
        try:
            meta = yaml.safe_load(m.group("fm")) or {}
        except yaml.YAMLError:
            meta = {}
        body = m.group("body")

    title = str(meta.get("title") or "").strip()
    if not title:
        h = re.search(r"^#\s+(.+)$", body, re.M)
        title = h.group(1).strip() if h else path.rsplit("/", 1)[-1]

    status_raw = str(meta.get("status") or "").strip().lower()
    if not status_raw:
        sm = re.search(r"^\s*[-*]?\s*\*{0,2}status:?\*{0,2}\s*:?\s*([a-z]+)", body, re.I | re.M)
        status_raw = sm.group(1).lower() if sm else ""
    try:
        status = DecisionStatus(status_raw)
    except ValueError:
        status = DecisionStatus.UNKNOWN

    deciders = meta.get("deciders") or []
    if isinstance(deciders, str):
        deciders = [deciders]
    deciders = [str(d).strip() for d in deciders if str(d).strip()]

    when = meta.get("date")
    if isinstance(when, str):
        try:
            when = date.fromisoformat(when[:10])
        except ValueError:
            when = None
    elif not isinstance(when, date):
        when = None

    local = str(meta.get("id") or "").strip() or (_NUM.search(path).group(1) if _NUM.search(path) else None)
    related = meta.get("related_design") or []
    if isinstance(related, str):
        related = [related]

    return Decision(
        global_id=global_id_for(title, body),
        source=DecisionSource.ADR,
        repo=repo, path=path, title=title, status=status,
        local_ref=local, deciders=deciders,
        ticket=str(meta.get("ticket")).strip() if meta.get("ticket") else None,
        when=when, related=[str(r) for r in related],
        open_gates=_gates(body, title=title), body=body,
    )


# TOP-LEVEL entries only. An entry's body carries its own bold sub-headings
# (`**What:**`, `**Where to start:**`), and an earlier version happily parsed
# those as decisions in their own right -- producing four entries titled
# "Where to start".
_TODO_ENTRY = re.compile(
    r"^(?P<indent>[ \t]*)[-*]\s+\*\*(?P<title>[^*\n]+?)\*\*(?P<rest>.*?)"
    r"(?=\n(?P=indent)[-*]\s+\*\*|\n#{1,3}\s|\Z)",
    re.S | re.M,
)
#: Field labels inside an entry, never entries themselves.
_TODO_FIELD_LABELS = frozenset({
    "what", "why", "pros", "cons", "context", "where to start", "depends on",
    "blocked by", "effort", "priority", "status", "note", "notes", "impact",
})


def parse_todos(repo: str, path: str, text: str) -> list[Decision]:
    """TODOS.md entries as deferred decisions.

    They earn a place here because they routinely record cross-repo blockers no
    ADR mentions -- the orchestrator's file names one scoped to Operator
    Workspace UI that "blocks Phase A.2 edit UI".
    """
    out: list[Decision] = []
    for m in _TODO_ENTRY.finditer(text):
        title = re.sub(r"\s+", " ", m.group("title")).strip(" —-:")
        rest = m.group("rest")
        if not title or len(title) < 6:
            continue
        if title.rstrip(":").lower() in _TODO_FIELD_LABELS:
            continue
        # A real entry is only top-level; nested ones are fields of their parent.
        if len(m.group("indent")) > 0:
            continue
        decided = re.search(r"\*\*DECIDED\s+(\d{4}-\d{2}-\d{2})[^*]*\*\*", rest)
        out.append(Decision(
            global_id=global_id_for(title, rest),
            source=DecisionSource.TODO,
            repo=repo, path=path, title=title,
            status=DecisionStatus.ACCEPTED if decided else DecisionStatus.DEFERRED,
            when=date.fromisoformat(decided.group(1)) if decided else None,
            open_gates=_gates(rest, title=title), body=rest,
        ))
    return out


def dedupe(decisions: list[Decision]) -> dict[str, list[Decision]]:
    """Group by global id. A forked ADR appears once, with every location it lives in."""
    grouped: dict[str, list[Decision]] = {}
    for d in decisions:
        grouped.setdefault(d.global_id, []).append(d)
    return grouped
