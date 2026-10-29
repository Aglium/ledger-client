"""Deterministic contract discovery: the graph, without a model.

Layer 0's card extraction needs a vendor and a key. This does not, and it finds
the contracts that matter most anyway -- topics, queues, declared HTTP paths and
tables are *literals in the source*, not something that has to be inferred.

The point is precision downstream. Linking a change to a decision through the
CONTRACT it touches is defensible ("this change publishes `invoice.events.v1`,
ADR-0009 governs that topic"). Linking through word overlap is not, and a gate
that blocks a merge has to be able to survive the argument.

Direction is read from the call site, not guessed: `publish("x")` produces,
`subscribe("x")` consumes. Where the surrounding code says neither, the edge is
recorded as a mention rather than upgraded to a claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ledger.core.estate import is_test_path


class Surface(str, Enum):
    TOPIC = "topic"            # event / queue / routing key
    HTTP = "http"              # declared endpoint path
    TABLE = "table"


class Edge(str, Enum):
    PUBLISHES = "publishes"
    CONSUMES = "consumes"
    MENTIONS = "mentions"      # present, but the call site does not say which way


@dataclass(frozen=True)
class ContractRef:
    surface: Surface
    name: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.surface.value}:{self.name}"


@dataclass
class ContractEdge:
    repo: str
    contract: ContractRef
    edge: Edge
    evidence_path: str
    evidence_line: str = ""


#: Bump whenever a change here alters what discover() returns. The stored index
#: keys on it: rows written by an older extractor are treated as stale and read
#: live until the next sync rescans them, so an upgrade never serves old answers.
EXTRACTOR_VERSION = "7"

# A dotted, lowercase literal is how most brokers name a topic.
_TOPIC = re.compile(r"['\"]((?:[a-z][a-z0-9_]*)(?:\.[a-z][a-z0-9_]*){1,5})['\"]")
#: Extensions and dotted module paths that look like topics but are not.
_NOT_TOPIC = re.compile(r"\.(py|json|ya?ml|md|txt|sql|html?|jsx?|tsx?|csv|ini|cfg|toml|lock|env|log)$", re.I)
#: A dotted literal handed to a translation or flash-message call is a
#: translation key, not a topic: `addSuccess('admin.claim.chat_message.sent')`,
#: `{{ 'x.y.z'|trans }}`, `t('x.y')`, a form field's `'label' => 'x.y'`. Found on
#: a real estate: four keys of one admin screen were reported as topics also
#: touched on an integration branch, and led a brief.
_TRANSLATED_BEFORE = re.compile(
    r"(?:\b(?:trans|transChoice|translate|gettext|ngettext|__|_t|t|\$t|i18n\.t|formatMessage|"
    r"addFlash|addSuccess|addDanger|addError|addWarning|addInfo|addNotice|flash|"
    r"Lang::get|@lang|lang)\s*\(\s*(?:['\"]\w+['\"]\s*,\s*)?"
    r"|['\"](?:label|placeholder|help|title|choice_label|empty_value|invalid_message)['\"]\s*=>\s*)$")
_TRANSLATED_AFTER = re.compile(r"^\s*\|\s*trans\b")
#: A name declares itself a messaging contract when one of its dotted parts
#: says so: `claim.events.v1`, `orders_topic`, `lead.created.event`. Matched on
#: the parts, not anywhere in the string: `chat_message` is a screen, not a
#: message bus.
_DECLARES = re.compile(
    r"^(?:events?|topics?|queues?|exchanges?|routing|messages?|commands?|intents?)$"
    r"|_(?:events?|topics?|queues?|exchanges?|commands?|intents?)$"
    r"|^(?:events?|topics?|queues?|exchanges?|commands?|intents?)_", re.I)


def declares_contract(name: str) -> bool:
    """Does the name itself say it is a topic, queue or event?"""
    return bool(_VERSIONED.search(name)) or any(_DECLARES.search(part) for part in name.split("."))


#: A dotted literal ending in a top-level domain is a hostname (`api.example.com`),
#: not a topic. Found on a real estate: two service hostnames were reported as
#: contracts shared between repos, and escalated a brief to review_with.
_HOSTNAME = re.compile(
    r"\.(com|net|org|io|dev|app|co|eu|uk|de|fr|lt|lv|ee|pl|es|it|nl|us|ca|au|cloud|"
    r"internal|local|localhost|lan|svc|cluster)$")

_PRODUCE = re.compile(
    r"\b(publish|produce|emit|dispatch|send_event|send|outbox|routing_key)\b", re.I)
_CONSUME = re.compile(
    r"\b(subscribe|consume|consumer|listen|handler|on_message|bind_queue|@\w*consume)\b", re.I)

_HTTP_DECL = re.compile(
    r"""(?:@\w+\.(?:get|post|put|patch|delete)|\.(?:get|post|put|patch|delete))\s*\(\s*"""
    r"""['\"](?P<path>/[\w{}/.\-]*)['\"]""", re.I)
#: A decorator declares a route (the server side). A plain method call on a
#: client -- `api.get("/x")` -- consumes one. Recording both as "publishes"
#: made every frontend look like a server and hid who calls what.
_HTTP_ROUTE_DECORATOR = re.compile(r"@\w+\.(?:get|post|put|patch|delete)\s*\(", re.I)
#: Client calls with template-literal URLs: `/invoices/${id}/required-items/`.
#: The way most frontends call APIs, and invisible to a quoted-string match.
_HTTP_TEMPLATE_CALL = re.compile(
    r"""(?:\bfetch|\.(?:get|post|put|patch|delete|request))\s*(?:<[^>]*>)?\s*\(\s*`(?P<url>[^`]*)`""", re.I)
#: Generated client types (openapi-typescript and similar): the consumer's own
#: copy of the producer's contract, one path per key.
#: The same decorator when formatters split it across lines:
#:     @router.get(
#:         "/{invoice_id}/required-items/",
#: Read over the whole file, since per-line matching never sees the path.
_HTTP_ROUTE_MULTILINE = re.compile(
    r"""@\w+\.(?:get|post|put|patch|delete)\s*\(\s*\n\s*['\"](?P<path>/[\w{}/.\-]*)['\"]""", re.I)
#: Test files mock routes and call them; neither makes the repo a server or a
#: client. On a real estate a UI test's mock handler made the UI "publish" an
#: endpoint the orchestrator serves.
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)/|\.(test|spec)\.[jt]sx?$|(^|/)test_[^/]+\.py$", re.I)
_GENERATED_PATH_KEY = re.compile(r"""^\s*['\"](?P<path>/[\w{}/.\-]*)['\"]\s*:\s*\{""", re.M)
_OPENAPI_PATH = re.compile(r"^\s{2}(?P<path>/[\w{}/.\-]*):\s*$", re.M)

#: Infrastructure endpoints every service exposes. Sharing one is not coupling,
#: it is a framework convention -- the HTTP equivalent of two classes both
#: having an __init__.
_INFRA_PATHS = frozenset({
    "/", "/health", "/healthz", "/healthcheck", "/health/live", "/health/ready",
    "/ready", "/readyz", "/live", "/livez", "/ping", "/status",
    "/metrics", "/docs", "/redoc", "/openapi.json", "/favicon.ico", "/version",
})

_TABLE = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+TABLE(?:\s+IF(?:\s+NOT)?\s+EXISTS)?\s+[`\"\[]?(?P<t>\w+)", re.I)

#: Not table names, and shared with `core.overlap`, which has its own table
#: regex and had the same defect two ways.
#:
#: `ALTER TABLE IF EXISTS x` was indexing a table called `if`, shared across
#: four repositories and printed in a real brief; a single letter is a query
#: alias (`FROM invoice c`) or a throwaway in a migration test. Seven of 814
#: table contracts on a real estate, every one visibly wrong, which costs more
#: trust than it saves work.
#:
#: The English function words are for `core.overlap`, which also matches
#: `UPDATE <name>` and `INSERT INTO <name>` because a diff often only writes
#: to a table. Those match prose too: "update to version 2" gave it a table
#: called `to` and "UPDATE or INSERT" one called `or`, and both turned up as
#: shared signals between unrelated repositories. `NEW` and `OLD` are trigger
#: row references, not tables.
NOT_A_TABLE = frozenset("""
if is as in on by of or and not null true false select from where case when then
end set table index view with union order group limit into values join left right
inner outer cross exists distinct having temp temporary unique primary key
new old
to the a an this that these those it its our your their my all some any every
each both more most less one two there here only also just now than while
because but for so
via failed success done ok none null_ true_ false_ value values data item items
row rows record records entry entries result results count total sum min max
""".split())
_TABLE_OP = re.compile(r"\b(?:create_table|add_column|drop_column|alter_column)\s*\(\s*['\"](?P<t>\w+)", re.I)
_TABLENAME = re.compile(r"__tablename__\s*=\s*['\"](?P<t>\w+)['\"]")


def _direction(line: str, window: str) -> Edge:
    produce, consume = _PRODUCE.search(line), _CONSUME.search(line)
    if produce and not consume:
        return Edge.PUBLISHES
    if consume and not produce:
        return Edge.CONSUMES
    if not produce and not consume:
        produce, consume = _PRODUCE.search(window), _CONSUME.search(window)
        if produce and not consume:
            return Edge.PUBLISHES
        if consume and not produce:
            return Edge.CONSUMES
    return Edge.MENTIONS


@dataclass
class Discovery:
    edges: list[ContractEdge] = field(default_factory=list)
    files_read: int = 0

    def contracts(self) -> set[ContractRef]:
        return {e.contract for e in self.edges}

    def by_contract(self) -> dict[ContractRef, list[ContractEdge]]:
        out: dict[ContractRef, list[ContractEdge]] = {}
        for e in self.edges:
            out.setdefault(e.contract, []).append(e)
        return out


def discover_in_text(repo: str, path: str, text: str) -> list[ContractEdge]:
    """Contracts named in one file, with direction read from the call site."""
    out: list[ContractEdge] = []
    lines = text.splitlines()
    low = path.lower()

    for i, line in enumerate(lines):
        window = "\n".join(lines[max(0, i - 3):i + 4])
        for m in _TOPIC.finditer(line):
            name = m.group(1)
            if _NOT_TOPIC.search(name) or _HOSTNAME.search(name) or name.count(".") > 5:
                continue
            if _TRANSLATED_BEFORE.search(line[:m.start()]) or _TRANSLATED_AFTER.search(line[m.end():]):
                continue
            out.append(ContractEdge(repo, ContractRef(Surface.TOPIC, name),
                                    _direction(line, window), path, line.strip()[:200]))
        for m in _HTTP_DECL.finditer(line):
            http_path = m.group("path")
            if http_path.rstrip("/").lower() in _INFRA_PATHS or http_path == "/":
                continue
            edge = Edge.PUBLISHES if _HTTP_ROUTE_DECORATOR.search(m.group(0)) else Edge.CONSUMES
            out.append(ContractEdge(repo, ContractRef(Surface.HTTP, http_path),
                                    edge, path, line.strip()[:200]))
        for m in _HTTP_TEMPLATE_CALL.finditer(line):
            http_path = _path_from_template(m.group("url"))
            if not http_path or http_path.rstrip("/").lower() in _INFRA_PATHS:
                continue
            out.append(ContractEdge(repo, ContractRef(Surface.HTTP, http_path),
                                    Edge.CONSUMES, path, line.strip()[:200]))
        for rx in (_TABLE, _TABLE_OP, _TABLENAME):
            for m in rx.finditer(line):
                name = m.group("t").lower()
                if len(name) < 2 or name in NOT_A_TABLE:
                    continue
                out.append(ContractEdge(repo, ContractRef(Surface.TABLE, name),
                                        Edge.PUBLISHES, path, line.strip()[:200]))

    for m in _HTTP_ROUTE_MULTILINE.finditer(text):
        if m.group("path").rstrip("/").lower() in _INFRA_PATHS:
            continue
        out.append(ContractEdge(repo, ContractRef(Surface.HTTP, m.group("path")),
                                Edge.PUBLISHES, path, "route decorator"))

    # A generated client is the consumer's copy of someone else's contract.
    if low.endswith(".d.ts") and ("interface paths" in text or "paths {" in text):
        for m in _GENERATED_PATH_KEY.finditer(text):
            if m.group("path").rstrip("/").lower() in _INFRA_PATHS or m.group("path") == "/":
                continue
            out.append(ContractEdge(repo, ContractRef(Surface.HTTP, m.group("path")),
                                    Edge.CONSUMES, path, "generated client type"))

    # A declared spec is unambiguous: its paths are published by definition.
    if re.search(r"(openapi|swagger)[^/]*\.ya?ml$", low):
        for m in _OPENAPI_PATH.finditer(text):
            if m.group("path").rstrip("/").lower() in _INFRA_PATHS:
                continue
            # A spec in a repo proves the contract is known there, not which side
            # it is on: UIs keep copies of the specs they call.
            out.append(ContractEdge(repo, ContractRef(Surface.HTTP, m.group("path")),
                                    Edge.MENTIONS, path, "declared in spec"))
    if _TEST_FILE.search(path) or is_test_path(path):
        out = [ContractEdge(e.repo, e.contract, Edge.MENTIONS, e.evidence_path, e.evidence_line)
               if e.contract.surface == Surface.HTTP else e for e in out]
    return out


def _path_from_template(url: str) -> str | None:
    """`${API_BASE}/invoices/${invoiceId}/required-items/` -> `/invoices/{invoiceId}/required-items/`."""
    url = re.sub(r"\$\{\s*([A-Za-z_][\w.]*)\s*\}", r"{\1}", url)
    url = re.sub(r"^\{[^}]*\}", "", url)                # a leading base-URL expression
    url = re.sub(r"^https?://[^/]+", "", url)
    url = url.split("?", 1)[0].split("#", 1)[0]
    if not url.startswith("/") or not re.fullmatch(r"/[\w{}/.\-]*", url):
        return None
    return url


#: A file that CREATES a table rather than using one. A repository that defines
#: a table in its own migrations owns that table.
_MIGRATION_PATH = re.compile(
    r"(^|/)(migrations?|alembic|doctrinemigrations|db/migrate|liquibase|flyway)(/|$)"
    r"|(^|/)[^/]*migration[^/]*\.\w+$", re.I)


def own_table(evidence_by_repo: dict) -> bool:
    """Do these repositories each have their OWN table of this name?

    A table name is not an identity. Fifteen repositories in one estate
    "shared" `audit_log`, and twelve "shared" `users` -- every one of them
    created from its own migrations, because they were generated from the same
    service template. They are not one table; they are fifteen tables with one
    name, and reporting them as coupled is confident noise.

    The discriminator is who CREATES it. A repository with a migration for the
    table owns a copy of it, so two such repositories are two databases. One
    migration plus models, seeds and queries elsewhere is the opposite: one
    owner and several users, which is what a shared table looks like.

    Measured on a real estate: `audit_log` had fourteen definers, `users` ten,
    `_schema_version` eight -- and `invoice` three, which is also right, because
    each service there does keep its own invoices table. `invoice_document` and
    `order_refund_row` had one apiece and are genuinely shared.

    The known cost: a table that really is shared and that two repositories
    both migrate -- one adding a column to the other's database -- reads as a
    collision. On this estate the per-service pattern dominates; where it does
    not, the rule would need provenance stronger than a path.
    """
    definers = [repo for repo, paths in evidence_by_repo.items()
                if any(_MIGRATION_PATH.search(p or "") for p in paths)]
    return len(definers) >= 2


def normalize_http(path: str) -> tuple[str, ...]:
    """Segments of a route with every placeholder spelling made equal.

    `{invoice_id}`, `{invoiceId}`, `:invoiceId`, `<invoice_id>` and `${invoiceId}` all
    name "some id here"; a trailing slash means nothing.
    """
    segs = []
    for seg in path.strip().strip("/").split("/"):
        if not seg:
            continue
        if re.fullmatch(r"\{[^}]*\}|:\w+|<[^>]*>|\$\{[^}]*\}", seg):
            segs.append("{}")
        else:
            segs.append(seg.lower())
    return tuple(segs)


_PATH_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """`invoices` and `invoice` are one resource; so are `categories` and `category`."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def path_tokens(path: str) -> set[str]:
    """The words of a source file's path, stemmed: `app/api/v1/invoices/assign.py`
    is {app, api, v1, invoice, assign, py}."""
    return {_stem(t) for t in _PATH_TOKEN.findall((path or "").lower())}


def route_words(name: str) -> list[str]:
    """A route's literal segments, stemmed, placeholders dropped; a segment
    like `required-items` gives both of its words."""
    return [_stem(t) for seg in normalize_http(name) if seg != "{}" for t in _PATH_TOKEN.findall(seg)]


def under_router(short: str, full: str, evidence_path: str) -> bool:
    """Is `short` the route `full`, declared relative to a router its file sits under?

    A server declares `/{invoice_id}/assign/` in `app/api/v1/invoices/assign.py`
    and mounts it under `/invoices`; a client calls `/invoices/{invoice_id}/assign/`.
    The two agree on a suffix too short to be trusted alone (`/{id}/assign`
    would match every assign endpoint in an estate), so the missing prefix
    has to be in the file's path. That is the real publisher a path-text
    match missed while it credited a service whose route merely shared the
    spelling.
    """
    s, f = normalize_http(short), normalize_http(full)
    if not s or len(s) >= len(f) or f[len(f) - len(s):] != s or not any(x != "{}" for x in s):
        return False
    # The segment the router is mounted at, nearest the route -- `invoices` in
    # `/internal/v1/invoices/{id}/assign/`. Mount prefixes further out
    # (`/internal/v1`) are set once in the application, not in the file.
    nearest = next((seg for seg in reversed(f[:len(f) - len(s)]) if seg != "{}"), None)
    if nearest is None:
        return False
    return {_stem(t) for t in _PATH_TOKEN.findall(nearest)} <= path_tokens(evidence_path)


def route_fit(name: str, evidence_path: str) -> int:
    """How many of a route's words its source file's path also names."""
    return len(set(route_words(name)) & path_tokens(evidence_path))


def http_equivalent(a: str, b: str) -> bool:
    """Are two route strings the same endpoint, seen from different sides?

    A server declares `/{invoice_id}/required-items/` under a router prefix;
    one client calls `/invoices/{id}/required-items/`, another the full
    `/internal/v1/invoices/{invoice_id}/required-items/`. They agree on a
    suffix. The shorter must carry at least two literal segments, or every
    `/{id}` would match everything.
    """
    x, y = normalize_http(a), normalize_http(b)
    if not x or not y:
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    # A long compound segment (`required-items`) is as specific as two plain
    # ones: routers declare `/{invoice_id}/required-items/` under a prefix, and
    # that is all the server file ever says.
    weight = sum(2 if (len(s) >= 8 and re.search(r"[-_]", s)) else 1 for s in short if s != "{}")
    if weight < 2:
        return x == y
    return long_[len(long_) - len(short):] == short


_READ_EXT = re.compile(r"\.(py|php|ts|tsx|js|jsx|go|rb|java|kt|sql|ya?ml|json)$", re.I)
_SKIP_PATH = re.compile(
    r"(^|/)(node_modules|vendor|\.venv|venv|dist|build|__pycache__|migrations?/versions)/", re.I)


#: A versioned suffix is how this estate marks a wire contract.
_VERSIONED = re.compile(r"\.v\d+$")
#: A prefix family this large is a translation namespace, not a set of topics.
_I18N_FAMILY_MIN = 12


def _module_paths(tree: list[str]) -> set[str]:
    """Dotted names that are really import paths, derived from the tree itself.

    `app.engine.clock.now_utc` looks exactly like a topic and is a module
    reference. The repo's own file list settles it without any heuristic.
    """
    out: set[str] = set()
    for path in tree:
        stem = re.sub(r"\.(py|rb|go|java|kt)$", "", path)
        if stem == path:
            continue
        dotted = stem.replace("/", ".")
        out.add(dotted)
        parts = dotted.split(".")
        for i in range(1, len(parts)):
            out.add(".".join(parts[:i]))
    return out


def _drop_noise(edges: list[ContractEdge], modules: set[str]) -> list[ContractEdge]:
    """Remove module references and translation namespaces from topic edges."""
    prefix_counts: dict[str, int] = {}
    for e in edges:
        if e.contract.surface is Surface.TOPIC:
            head = ".".join(e.contract.name.split(".")[:2])
            prefix_counts[head] = prefix_counts.get(head, 0) + 1
    i18n = {h for h, n in prefix_counts.items() if n >= _I18N_FAMILY_MIN}

    kept: list[ContractEdge] = []
    for e in edges:
        if e.contract.surface is not Surface.TOPIC:
            kept.append(e)
            continue
        name = e.contract.name
        if name in modules or name.split(".")[0] in {"app", "src", "tests", "lib"}:
            continue
        if ".".join(name.split(".")[:2]) in i18n and not _VERSIONED.search(name):
            continue
        # A bare mention is only a contract when the name itself declares one.
        if e.edge is Edge.MENTIONS and not declares_contract(name):
            continue
        kept.append(e)
    return kept


def discover(
    repo: str, tree: list[str], read, *, max_files: int = 400, per_file_limit: int = 200_000,
    read_many=None,
) -> Discovery:
    """Walk a repo's tree and collect every contract edge it can prove.

    `read_many(paths) -> {path: text}` is used when given, so an adapter that
    can batch reads does the whole repo in one round trip.
    """
    d = Discovery()
    wanted = [p for p in tree if not _SKIP_PATH.search(p) and _READ_EXT.search(p)][:max_files]
    if read_many is not None:
        texts = read_many(wanted)
    else:
        texts = {}
        for path in wanted:
            try:
                texts[path] = read(path)
            except Exception:
                continue
    for path in wanted:
        text = texts.get(path)
        if not text or len(text) > per_file_limit:
            continue
        d.files_read += 1
        d.edges.extend(discover_in_text(repo, path, text))
    d.edges = _drop_noise(d.edges, _module_paths(tree))
    return d


def resolve_direction(edges: list[ContractEdge]) -> Edge:
    """Collapse a repo's edges for one contract into a single direction.

    A repo that both publishes and consumes a topic is publishing it -- the
    consume is usually its own test or a round-trip. A bare mention never
    outranks a real call site.
    """
    kinds = {e.edge for e in edges}
    if Edge.PUBLISHES in kinds:
        return Edge.PUBLISHES
    if Edge.CONSUMES in kinds:
        return Edge.CONSUMES
    return Edge.MENTIONS
