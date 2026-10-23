"""Documentation as evidence: which page documents a contract, and when it was
last touched.

The estate's documentation is not absent -- it is a wiki, mirrored into a
repository as markdown, and it is behind the code. That makes it exactly the
wrong thing to send an agent to read, and exactly the right thing to *name*:
a change that removes a field from an endpoint an API page documents needs
that page updated, and the page's own timestamp says how far behind it
already is.

Matching is deliberately narrow. A wiki page is prose, and a table called
`invoice` or a route called `/admin` appears in prose constantly: matching
literally linked a database table to 147 unrelated pages. Two rules cut that
to references people would recognise as references:

  code only     the name must appear in a fenced block or between backticks.
                Documentation cites a contract as code; prose mentions the
                idea, not the contract.
  separated     a name with no separator (`invoice`, `vendor`, `document`) is
                an English word before it is an identifier, and is skipped.
                It costs real references and buys the rest being trustworthy.

Routes are compared by normalised segments, so a page documenting
`/invoices/{invoiceId}` matches the route defined as `/invoices/{invoice_id}` and a
trailing slash never decides anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from ledger.core.contracts import ContractRef, Surface, normalize_http

_FENCE = re.compile(r"^\s*(```|~~~)")
_SPAN = re.compile(r"`([^`]+)`")
#: A token as documentation writes one: identifiers, dotted topic names and
#: routes with placeholders. Trailing punctuation is left behind.
_TOKEN = re.compile(r"[\w./{}-]*[\w}]")
_SEPARATED = re.compile(r"[._/-]")
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.M)


@dataclass(frozen=True)
class DocPage:
    path: str
    title: str
    body: str
    url: str | None = None
    #: When the page itself was last updated, where the source records it.
    #: A mirrored wiki keeps the wiki's timestamp, which is the one that
    #: matters: a bulk re-export does not make a page current.
    updated: datetime | None = None


def _parsed_date(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_page(path: str, text: str) -> DocPage:
    """A markdown page, with whatever metadata it carries.

    Mirrored wikis put the page id, url and last update in YAML frontmatter.
    A page without it still has a first heading and a file name.
    """
    meta, body = {}, text
    m = _FRONTMATTER.match(text)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            meta = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            meta = {}
        body = text[m.end():]
    heading = _HEADING.search(body)
    title = str(meta.get("title") or (heading.group(1) if heading else "")).strip()
    return DocPage(path=path, title=title or path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                   body=body, url=str(meta["url"]) if meta.get("url") else None,
                   updated=_parsed_date(meta.get("updated") or meta.get("last_updated")))


def index_key(ref: ContractRef) -> tuple | str | None:
    """The key a contract is looked up by, or None if it cannot be looked up.

    A route of placeholders only (`/{asset_id}`) normalises to "one segment,
    anything" and would match every one-segment route in every document.
    """
    if ref.surface == Surface.HTTP:
        segs = normalize_http(ref.name)
        return segs if any(s != "{}" for s in segs) else None
    return ref.name.lower() if _SEPARATED.search(ref.name) else None


@dataclass(frozen=True)
class DocMention:
    ref: ContractRef
    line_no: int
    line: str
    #: `fence` (inside a code block) or `code` (between backticks).
    kind: str


def mentions(page: DocPage, by_key: dict) -> list[DocMention]:
    """Every contract in `by_key` this page cites as code, once each.

    `by_key` maps the key from `index_key` to a ContractRef.
    """
    out: dict[object, DocMention] = {}
    in_fence = False
    for line_no, line in enumerate(page.body.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        chunks = [(line, "fence")] if in_fence else [(s, "code") for s in _SPAN.findall(line)]
        for chunk, kind in chunks:
            for token in _TOKEN.findall(chunk):
                if not _SEPARATED.search(token):
                    continue
                key = normalize_http(token) if token.startswith("/") else token.lower()
                ref = by_key.get(key)
                if ref is not None and key not in out:
                    out[key] = DocMention(ref, line_no, line.strip()[:200], kind)
    return list(out.values())
