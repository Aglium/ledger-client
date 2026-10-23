"""Topic names reached through a variable.

Services rarely write a topic name where they use it. They bind it once --
an env var in deployment values, a setting with a default, a framework
parameter -- and use the binding everywhere else:

    helm/values-stage.yaml   KAFKA_TOPIC_INVOICE_EVENTS: "app.invoice-events"
    app/config.py            INVOICE_EVENTS_TOPIC = _topic("KAFKA_TOPIC_INVOICE_EVENTS", "invoice.events.v1")
    app/relay.py             producer.send(INVOICE_EVENTS_TOPIC, event)
    services.yml             - '%kafka_topic_invoice_events%'     # wired into ...EventConsumer

Literal matching sees none of the usage, and treats the two names as two
topics. This module finds the bindings, treats every value bound to one
identifier as one topic, and reads direction at the places the identifier is
used.

Only identifiers that say what they are -- containing `topic`, `queue`,
`stream` or `channel` -- are bindings. That single rule is what keeps an
arbitrary `NAME = "a.b"` from becoming a topic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOPIC_IDENT = re.compile(r"topic|queue|stream|channel", re.I)
#: Dotted names, hyphens allowed: `invoice.events.v1`, `app-name.invoice-events`.
_TOPIC_VALUE = re.compile(r"^[a-z0-9][a-z0-9_\-]*(?:\.[a-z0-9][a-z0-9_\-]*)+$")
#: A dotted name that is a filename or a hostname, not a channel. The
#: document and media extensions were added after `agreement.pdf` and
#: `invoice.pdf` were reported as shared topics linking five unrelated
#: branches: a diff that attaches a document names the file, and the file
#: looks exactly like `invoice.created` to a regex.
_NOT_TOPIC_VALUE = re.compile(
    r"\.(py|json|ya?ml|md|txt|sql|html?|jsx?|tsx?|php|csv|ini|cfg|toml|lock|env|log"
    r"|pdf|docx?|xlsx?|pptx?|csv|rtf|odt"
    r"|png|jpe?g|gif|svg|webp|ico|bmp|tiff?|mp[34]|wav|avi|mov|webm"
    r"|zip|gz|tar|tgz|bz2|xz|7z|rar|jar|war|whl|pkl"
    r"|woff2?|ttf|eot|otf|css|scss|less|map|min"
    r"|com|net|org|io|dev|svc|local)$", re.I)

_ENV_DEFAULT = re.compile(
    r"""(?:(?P<var>[A-Za-z_]\w*)\s*(?::\s*[\w\[\], ]+)?\s*=\s*)?[\w.]*"""
    r"""(?:_topic|getenv|environ\.get|env|get_env|config)\s*\(\s*['"](?P<key>[A-Za-z_][\w.]*)['"]\s*,\s*"""
    r"""['"](?P<value>[^'"]+)['"]""")
_ASSIGN = re.compile(
    r"""^\s*(?:export\s+|const\s+|let\s+|var\s+|public\s+|private\s+|protected\s+|static\s+|final\s+)*"""
    r"""['"]?(?P<key>[A-Za-z_][\w.\-]*)['"]?\s*(?::\s*[\w\[\], |]+)?\s*(?:=|:)\s*['"]?(?P<value>[^'"\s#,;]+)['"]?""")

CONSUME_HINTS = ("consum", "subscrib", "listen", "on_message", "handler", "poll")
PRODUCE_HINTS = ("publish", "produc", "emit", "outbox", "relay", "send(", ".send", "dispatch")


def norm(ident: str) -> str:
    """`KAFKA_TOPIC_INVOICE_EVENTS`, `kafka_topic_invoice_events` and
    `kafka.topic.invoice-events` name the same binding."""
    return re.sub(r"[^a-z0-9]+", "_", ident.lower()).strip("_")


def _is_topic(value: str) -> bool:
    return bool(_TOPIC_VALUE.match(value)) and not _NOT_TOPIC_VALUE.search(value)


@dataclass(frozen=True)
class Binding:
    ident: str          # normalised identifier
    raw: str            # as written
    value: str          # the topic name it is bound to
    path: str


@dataclass(frozen=True)
class Usage:
    ident: str
    edge: str           # publishes | consumes | mentions
    path: str
    line: str


def bindings_in(path: str, text: str) -> list[Binding]:
    """Every identifier this file binds to a topic name."""
    out: list[Binding] = []
    for line in text.splitlines():
        for m in _ENV_DEFAULT.finditer(line):
            value = m.group("value")
            if not _is_topic(value):
                continue
            for raw in filter(None, (m.group("key"), m.group("var"))):
                if _TOPIC_IDENT.search(raw):
                    out.append(Binding(norm(raw), raw, value, path))
        m = _ASSIGN.match(line)
        if m and _TOPIC_IDENT.search(m.group("key")) and _is_topic(m.group("value")):
            out.append(Binding(norm(m.group("key")), m.group("key"), m.group("value"), path))
    return list(dict.fromkeys(out))


def usages_in(path: str, text: str, idents: dict[str, set[str]]) -> list[Usage]:
    """Where bound identifiers are used, and which way.

    `idents` maps a normalised identifier to the spellings seen for it.
    Direction comes from the lines around the use: wiring a parameter into
    `InvoiceStatusEventConsumer` consumes; handing a setting to a producer
    publishes. A use with no signal, or both, is only a mention.
    """
    if not idents:
        return []
    lines = text.splitlines()
    spellings = {}
    for ident, raws in idents.items():
        for raw in raws | {ident, ident.upper()}:
            spellings[raw] = ident
    alternation = "|".join(sorted((re.escape(s) for s in spellings), key=len, reverse=True))
    # `settings.KAFKA_EVENTS_TOPIC` is the commonest use of a binding, so a
    # leading dot is allowed.
    word = re.compile(rf"(?<!\w){'(' + alternation + ')'}(?!\w)|%(?P<param>[\w.\-]+)%", re.I)
    out: list[Usage] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(lines):
        for m in word.finditer(line):
            token = m.group("param") or m.group(1)
            ident = spellings.get(token) or norm(token)
            if ident not in idents or (ident, i) in seen:
                continue
            # The binding line itself is not a use.
            rest = line[m.end():]
            if re.match(r"""\s*(?::\s*[\w\[\], |]+)?\s*[:=]\s*['"]?[a-z0-9]""", rest, re.I) and "(" not in line[:m.start()]:
                continue
            seen.add((ident, i))
            window = "\n".join(lines[max(0, i - 6):i + 7]).lower()
            consume = any(h in window for h in CONSUME_HINTS)
            produce = any(h in window for h in PRODUCE_HINTS)
            edge = "consumes" if consume and not produce else "publishes" if produce and not consume else "mentions"
            out.append(Usage(ident, edge, path, line.strip()[:200]))
    return out


def alias_groups(bindings: list[Binding]) -> list[set[str]]:
    """Topic names that are one topic: every value bound to one identifier
    within a repo. Union-find, so chains collapse."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_ident: dict[str, list[str]] = {}
    for b in bindings:
        by_ident.setdefault(b.ident, []).append(b.value)
        find(b.value)
    for values in by_ident.values():
        for v in values[1:]:
            parent[find(v)] = find(values[0])
    groups: dict[str, set[str]] = {}
    for v in parent:
        groups.setdefault(find(v), set()).add(v)
    return [g for g in groups.values() if len(g) > 1]
