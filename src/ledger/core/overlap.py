"""The control arm: deterministic overlap, no model calls.

This exists to be *beaten*. Without a cheap baseline, the accuracy numbers from
any later inference-based layer are good relative to nothing, and nobody can
answer whether the expensive part earned its cost. So this is built first and
kept honest: if it catches most of what actually breaks, that is a finding, not
a failure.

Signals are extracted from a diff by regex, in rough order of how much they
mean when two changes share one:

  table      two changes touching `invoices` is close to a real collision
  migration  two migrations in flight at once is close to a guaranteed one
  symbol     a shared function or class name, decent signal
  path       the same file, weakest -- shared config and lockfiles are noise

Nothing here knows about any company's conventions; the noise patterns come
from config.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ledger.core.bindings import _NOT_TOPIC_VALUE
from ledger.core.contracts import NOT_A_TABLE
from enum import Enum


class Kind(str, Enum):
    PATH = "path"
    MIGRATION = "migration"
    SYMBOL = "symbol"          # a DEFINED name
    TABLE = "table"
    EVENT = "event"            # topic / routing key / queue name
    KEY = "key"                # payload field name
    IDENTIFIER = "identifier"  # a name USED, wherever it was defined


#: Weight per signal kind, used only to rank results. Deliberately coarse --
#: pretending to more precision than a regex earns would be dishonest.
WEIGHT: dict[Kind, int] = {
    Kind.EVENT: 9,          # a shared topic is close to proof of coupling
    Kind.TABLE: 8,
    Kind.MIGRATION: 6,
    Kind.SYMBOL: 3,
    Kind.KEY: 3,
    Kind.IDENTIFIER: 2,     # weak alone, decisive in numbers
    Kind.PATH: 1,
}

_MIGRATION_HINTS = ("migration", "migrations", "alembic", "versions", "doctrine")

#: Symbol names so common that sharing one means nothing. Measured, not guessed:
#: an early benchmark linked unrelated branches in different repos purely through
#: `__init__`, `add` and `clear`. Unqualified symbol
#: matching is the cheap arm's precision problem -- paths and tables behave.
_UBIQUITOUS = frozenset({
    "__init__", "__str__", "__repr__", "__eq__", "__hash__", "__enter__", "__exit__",
    "setUp", "tearDown", "setup", "teardown", "main", "run", "handle", "execute",
    "add", "get", "set", "put", "post", "delete", "update", "create", "remove",
    "clear", "reset", "start", "stop", "close", "open", "read", "write", "send",
    "build", "parse", "render", "process", "validate", "check", "init", "load",
    "save", "apply", "call", "next", "value", "name", "data", "result", "config",
    "upgrade", "downgrade",
})

# Definition sites across the four languages in play. Anchored to a line start
# (after diff markers) so a mention inside a string is not mistaken for a def.
_SYMBOL_RE = re.compile(
    r"^[+-]?\s*(?:"
    r"(?:async\s+)?def\s+(?P<py>\w+)"
    r"|class\s+(?P<cls>\w+)"
    r"|(?:export\s+)?(?:async\s+)?function\s+(?P<js>\w+)"
    r"|(?:export\s+)?(?:const|let|var)\s+(?P<jsvar>\w+)\s*="
    r"|(?:public|private|protected)\s+(?:static\s+)?function\s+(?P<php>\w+)"
    r")",
    re.MULTILINE,
)
# Topic / routing key / queue names. Both the dotted-literal form and the
# argument position of a publish or subscribe call.
_EVENT_LITERAL_RE = re.compile(r"['\"]([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,4})['\"]")
_EVENT_CALL_RE = re.compile(
    r"(?:publish|subscribe|consume|emit|send|dispatch|routing_key|topic|queue|exchange|channel)"
    r"\s*[=(]\s*['\"]([A-Za-z][\w.\-/]{2,60})['\"]", re.I)

# Payload keys. Dict literals and JSON bodies are where cross-service field
# names actually live -- `orders[].customerId` is a key, never a def.
_KEY_RE = re.compile(r"['\"]([a-zA-Z_][a-zA-Z0-9_]{3,40})['\"]\s*:")

# Any identifier USED. InvoiceDocument is defined in core and referenced in
# leads; definition-only matching could never see that, which is exactly the
# coupling the first benchmark missed.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{4,60})\b")

#: `UPDATE` and `INSERT INTO` are here because a diff that touches a table
#: often only writes to it. They also match English: "update to version 2"
#: produced a table called `to`, and "UPDATE or INSERT" one called `or`, both
#: of which turned up as shared signals between unrelated repositories.
#: `NOT_A_TABLE` is the same rejection the contract extractor uses, imported
#: rather than copied so the two cannot drift.
_TABLE_RE = re.compile(
    r"\b(?:(?:CREATE|ALTER|DROP)\s+TABLE(?:\s+IF(?:\s+NOT)?\s+EXISTS)?|INSERT\s+INTO|UPDATE)\s+"
    r"[`\"\[]?(?P<t>\w+)[`\"\]]?",
    re.IGNORECASE,
)
_OP_RE = re.compile(r"\b(?:create_table|drop_table|add_column|drop_column|alter_column)\s*\(\s*['\"](?P<t>\w+)", re.I)

#: Dotted names that are configuration, not channels. Helm and chart values
#: are the whole of this: `app.worker`, `app.fullname`, `app.name` linked
#: `new-service` -- a service *template* -- to three product repositories.
_NOT_A_TOPIC = re.compile(
    r"\A(app|chart|release|global|image|service|ingress|values|metadata|spec|"
    r"resources|limits|requests|labels|annotations|env|config|secret|volume|"
    r"probe|port|host|path|version|replica|selector|template)\.", re.I)


@dataclass(frozen=True)
class Signal:
    kind: Kind
    value: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind.value}:{self.value}"


@dataclass(frozen=True)
class DiffFile:
    path: str
    body: str = ""


def _is_migration(path: str) -> bool:
    low = path.lower()
    return any(h in low.split("/") or h in low for h in _MIGRATION_HINTS)


def extract_signals(files: list[DiffFile], *, ignore: tuple[str, ...] = ()) -> set[Signal]:
    """Deterministic signals for one change. Same input, same output, always."""
    out: set[Signal] = set()
    for f in files:
        if any(re.search(pat, f.path) for pat in ignore):
            continue
        out.add(Signal(Kind.PATH, f.path))
        if _is_migration(f.path):
            out.add(Signal(Kind.MIGRATION, f.path.rsplit("/", 1)[-1]))
        for m in _SYMBOL_RE.finditer(f.body):
            name = next((g for g in m.groupdict().values() if g), None)
            if name and len(name) > 3 and name not in _UBIQUITOUS:
                out.add(Signal(Kind.SYMBOL, name))
        for rx in (_TABLE_RE, _OP_RE):
            for m in rx.finditer(f.body):
                name = m.group("t").lower()
                if len(name) < 2 or name in NOT_A_TABLE:
                    continue
                out.add(Signal(Kind.TABLE, name))
        for rx in (_EVENT_LITERAL_RE, _EVENT_CALL_RE):
            for m in rx.finditer(f.body):
                val = m.group(1)
                if not ("." in val or "/" in val or "_" in val):
                    continue
                # A dotted name is not a topic just because it is dotted.
                # `agreement.pdf` and `invoice.pdf` are filenames and linked
                # five unrelated branches; `app.worker` and `app.fullname` are
                # Helm template keys and linked a service template to three
                # repositories. `core.bindings` already had the answer and
                # this had its own, weaker one.
                if _NOT_TOPIC_VALUE.search(val) or _NOT_A_TOPIC.match(val.lower()):
                    continue
                out.add(Signal(Kind.EVENT, val.lower()))
        for m in _KEY_RE.finditer(f.body):
            k = m.group(1)
            if k not in _UBIQUITOUS:
                out.add(Signal(Kind.KEY, k))
        for m in _IDENT_RE.finditer(f.body):
            name = m.group(1)
            if name not in _UBIQUITOUS and not name.isupper():
                out.add(Signal(Kind.IDENTIFIER, name))
    return out


def drop_frequent(
    population: dict[tuple[str, str], set[Signal]], *, max_change_fraction: float = 0.10
) -> dict[tuple[str, str], set[Signal]]:
    """Drop signals common across CHANGES, not just across repos.

    This is the correction that makes the arm useful rather than merely loud.
    Two coupled repos share their whole domain vocabulary -- `Invoice`,
    `DocumentType`, `Signature` appear in nearly every change either one makes.
    Matching on those rediscovers "these repos are coupled" once per pair, which
    is a Layer 0 fact and not an event worth interrupting anyone about.

    What is worth an alert is a RARE shared signal: `invoice_agreements_signature`
    in two changes, not `Invoice` in thirty-four. So a signal appearing in more
    than `max_change_fraction` of changes carries no information and is removed.

    EVENT and TABLE stay exempt: a topic or table shared widely is a large
    coupling, not boilerplate.
    """
    if not population:
        return population
    freq: dict[Signal, int] = {}
    for sigs in population.values():
        for s in sigs:
            freq[s] = freq.get(s, 0) + 1
    ceiling = max(2, int(len(population) * max_change_fraction))
    common = {
        s for s, n in freq.items()
        if n > ceiling and s.kind not in (Kind.EVENT, Kind.TABLE)
    }
    return {k: (v - common) for k, v in population.items()}


def drop_common(
    population: dict[tuple[str, str], set[Signal]], *, max_repos: int = 3
) -> dict[tuple[str, str], set[Signal]]:
    """Remove signals that appear in too many repos to mean anything.

    Document-frequency filtering, not a hand-written stoplist. A token two repos
    share is evidence; a token six repos share is boilerplate -- a logging
    helper, a framework name, a language builtin. This self-calibrates per
    estate instead of encoding one company's vocabulary, which is what keeps
    `core/` portable.

    EVENT and TABLE are exempt: a queue name or a table in six repos is not
    noise, it is a very large coupling.
    """
    repos_per: dict[Signal, set[str]] = {}
    for (repo, _), sigs in population.items():
        for s in sigs:
            repos_per.setdefault(s, set()).add(repo)
    common = {
        s for s, repos in repos_per.items()
        if len(repos) > max_repos and s.kind not in (Kind.EVENT, Kind.TABLE)
    }
    return {k: (v - common) for k, v in population.items()}


@dataclass
class OverlapHit:
    repo: str
    ref: str
    shared: set[Signal] = field(default_factory=set)

    #: Identifiers are weak individually and enormous in number: two files in
    #: the same language share hundreds. Uncapped, raw volume swamps a shared
    #: event or table, which are the signals that actually mean something.
    IDENTIFIER_CAP = 10

    @property
    def score(self) -> int:
        total = 0
        ident = 0
        for s in self.shared:
            if s.kind is Kind.IDENTIFIER:
                ident += 1
                if ident > self.IDENTIFIER_CAP:
                    continue
            total += WEIGHT[s.kind]
        return total

    def by_kind(self) -> dict[Kind, list[str]]:
        grouped: dict[Kind, list[str]] = defaultdict(list)
        for s in self.shared:
            grouped[s.kind].append(s.value)
        return {k: sorted(v) for k, v in grouped.items()}


def find_overlaps(
    subject: tuple[str, str],
    subject_signals: set[Signal],
    population: dict[tuple[str, str], set[Signal]],
    *,
    cross_repo_only: bool = True,
    min_score: int = 1,
) -> list[OverlapHit]:
    """Changes sharing at least one signal with the subject, best first."""
    hits: list[OverlapHit] = []
    for (repo, ref), signals in population.items():
        if (repo, ref) == subject:
            continue
        if cross_repo_only and repo == subject[0]:
            continue
        shared = subject_signals & signals
        if not shared:
            continue
        hit = OverlapHit(repo=repo, ref=ref, shared=shared)
        if hit.score >= min_score:
            hits.append(hit)
    hits.sort(key=lambda h: (-h.score, h.repo, h.ref))
    return hits
