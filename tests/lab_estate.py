"""A labelled estate for measuring what a replay finds without ticket keys.

Ten repositories with real, dated git history. Each merged change carries the
answer a person who knew the work would give: whether Ledger should say
something about another repository, and how strong that claim should be. Half
the changes that matter carry no ticket key anywhere, which is the gap this
measures; the rest are decoys built to fool a looser rule.

    uv run python tests/lab_estate.py        # prints the metrics as JSON

`tests/test_lab.py` holds the client to them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "ci@example.com",
        "PATH": os.environ["PATH"]}


def _when(days_ago: int, hour: int = 12, minute: int = 0) -> str:
    d = datetime.combine(date.today() - timedelta(days=days_ago), datetime.min.time())
    return d.replace(hour=hour, minute=minute).isoformat()


def _git(p: Path, *args: str, at: str, email: str = "ann@example.com") -> None:
    subprocess.run(["git", "-C", str(p), *args], check=True, capture_output=True,
                   env={**_ENV, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at,
                        "GIT_AUTHOR_EMAIL": email, "HOME": str(p)})


def _write(p: Path, files: dict[str, str | None]) -> None:
    for f, text in files.items():
        if text is None:
            (p / f).unlink()
            continue
        (p / f).parent.mkdir(parents=True, exist_ok=True)
        (p / f).write_text(text)


def _repo(root: Path, name: str, files: dict[str, str]) -> Path:
    p = root / name
    p.mkdir(parents=True)
    at = _when(40)
    _git(p, "init", "-q", "-b", "main", at=at)
    _git(p, "remote", "add", "origin", f"git@github.com:acme/{name}.git", at=at)
    _write(p, files)
    _git(p, "add", "-A", at=at)
    _git(p, "commit", "-q", "-m", "start", at=at)
    return p


@dataclass(frozen=True)
class Label:
    """What someone who knew the work would say about one merged change."""

    flag: bool                       # should Ledger say something about another repository?
    strength: str | None = None      # conflict | dependency | correlation, when it should
    ticket: bool = False             # does a ticket key link it?
    why: str = ""


#: Branches are created, worked on, and merged in this order, so each one's
#: start and merge day are exact. (repo, branch, author, start, merge, files)
#: start and merge are (days_ago, hour, minute).
def _plan() -> list[tuple]:
    return [
        # A ticket links two halves: the baseline already finds it.
        ("orders", "feat/ABC-7-gift-notes", "ann@example.com", (22, 10, 0), (20, 15, 0),
         {"app/gift.py": "def gift_note(order):\n    return order.note\n"}),
        ("mailer", "feat/ABC-7-gift-note-email", "bob@example.com", (22, 11, 0), (19, 15, 0),
         {"app/gift_mail.py": "def render_gift(note):\n    return note\n"}),
        # No ticket: a producer drops a field a consumer on main still reads.
        ("orders", "drop-discount-code", "carl@example.com", (15, 10, 0), (14, 15, 0),
         {"app/events.py":
          "def emit_order(order):\n"
          "    producer.send(\"order.placed.v1\", {\"order_id\": order.id,\n"
          "                                    \"total_cents\": order.total})\n"}),
        # No ticket: two halves of one feature, same author, forty minutes apart.
        ("checkout", "abandoned-cart-events", "dana@example.com", (12, 10, 0), (10, 15, 0),
         {"app/cart.py":
          "def abandon(cart):\n"
          "    producer.send(\"cart.abandoned.v1\", {\"cart_ref\": cart.ref, \"reminder_at\": cart.at})\n"}),
        ("crm", "cart-followups", "dana@example.com", (12, 10, 40), (10, 16, 0),
         {"app/followups.py":
          "@consumer.subscribe(\"cart.abandoned.v1\")\n"
          "def on_abandoned(payload):\n"
          "    schedule(payload[\"cart_ref\"], payload[\"reminder_at\"])\n"}),
        # Decoy: the same author, the same day, six hours later, unrelated.
        ("mailer", "tidy-templates", "dana@example.com", (12, 16, 30), (11, 15, 0),
         {"templates/footer.txt": "Thanks for shopping with us.\n"}),
        # No ticket: one removes a route while another starts calling it.
        ("catalog", "retire-price-endpoint", "erin@example.com", (8, 10, 0), (6, 15, 0),
         {"app/api.py":
          "@router.get(\"/v1/products/{id}\")\n"
          "def product(id):\n"
          "    return load(id)\n"}),
        ("storefront", "show-live-price", "fred@example.com", (7, 10, 0), (5, 15, 0),
         {"app/price.js":
          "export async function livePrice(id) {\n"
          "  return api.get(`/v1/products/${id}/price`);\n"
          "}\n"}),
        # No ticket: a producer changes how it publishes what another consumes.
        ("payments", "settle-in-batches", "gina@example.com", (9, 10, 0), (8, 15, 0),
         {"app/settle.py":
          "def settle(batch):\n"
          "    for p in batch:\n"
          "        producer.send(\"payment.settled.v1\", {\"payment_ref\": p.ref, \"batch_ref\": batch.ref})\n"}),
        # Decoy: a table every service has its own copy of.
        ("reports", "audit-retention", "hank@example.com", (6, 10, 0), (5, 15, 0),
         {"migrations/002_retention.sql": "ALTER TABLE audit_log ADD COLUMN kept_until date;\n"}),
        # Decoy: a key removed from a dict that is no contract's payload.
        ("orders", "rename-internal-helper", "carl@example.com", (4, 10, 0), (3, 15, 0),
         {"app/internal.py": "def flags():\n    return {\"internal_mode\": True}\n"}),
        # Decoy: documentation only.
        ("accounts", "fix-typo", "ivan@example.com", (3, 10, 0), (2, 15, 0),
         {"README.md": "Accounts service. Consumes settlements.\n"}),
        # Decoy: the same author, half an hour later, more documentation elsewhere.
        ("catalog", "catalog-docs", "ivan@example.com", (3, 10, 30), (2, 16, 0),
         {"docs/README.md": "Catalog serves product data.\n"}),
        # A producer drops a field that only an unrelated topic's reader uses.
        # Related to mailer over the topic; nothing of mailer's breaks.
        ("orders", "drop-total-cents", "carl@example.com", (2, 10, 0), (1, 15, 0),
         {"app/events.py":
          "def emit_order(order):\n"
          "    producer.send(\"order.placed.v1\", {\"order_id\": order.id})\n"}),
    ]


LABELS: dict[str, Label] = {
    "feat/ABC-7-gift-notes": Label(True, "correlation", ticket=True, why="same ticket, other half"),
    "feat/ABC-7-gift-note-email": Label(True, "correlation", ticket=True, why="same ticket, other half"),
    "drop-discount-code": Label(True, "conflict", why="mailer still reads discount_code"),
    "abandoned-cart-events": Label(True, "dependency", why="crm starts consuming what this publishes"),
    "cart-followups": Label(True, "dependency", why="checkout starts publishing what this consumes"),
    "tidy-templates": Label(False, why="same author, same day, unrelated"),
    "retire-price-endpoint": Label(False, why="nobody called the route the day it started"),
    "show-live-price": Label(True, "conflict", why="calls a route catalog is removing"),
    "settle-in-batches": Label(True, "dependency", why="accounts consumes payment.settled.v1"),
    "audit-retention": Label(False, why="billing has its own audit_log"),
    "rename-internal-helper": Label(False, why="the removed key is no contract's"),
    "fix-typo": Label(False, why="documentation only"),
    "catalog-docs": Label(False, why="same author, minutes apart, both documentation"),
    "drop-total-cents": Label(True, "dependency",
                              why="mailer consumes the topic; total_cents is read only from "
                                  "another topic"),
}


def build(root: Path) -> Path:
    """Create the estate under `root`; returns the config path."""
    src = root / "src"
    repos = {
        "orders": _repo(src, "orders", {
            "app/events.py":
                "def emit_order(order):\n"
                "    producer.send(\"order.placed.v1\", {\"order_id\": order.id,\n"
                "                                    \"discount_code\": order.discount,\n"
                "                                    \"total_cents\": order.total})\n",
            "app/internal.py": "def flags():\n    return {\"internal_flag\": True}\n"}),
        "mailer": _repo(src, "mailer", {
            "app/orders.py":
                "@consumer.subscribe(\"order.placed.v1\")\n"
                "def on_order(payload):\n"
                "    send_receipt(payload[\"order_id\"], payload[\"discount_code\"])\n"}),
        "checkout": _repo(src, "checkout", {"app/cart.py": "def abandon(cart):\n    pass\n"}),
        "crm": _repo(src, "crm", {"app/contacts.py": "def contact(c):\n    return c\n"}),
        "catalog": _repo(src, "catalog", {
            "app/api.py":
                "@router.get(\"/v1/products/{id}\")\n"
                "def product(id):\n"
                "    return load(id)\n\n"
                "@router.get(\"/v1/products/{id}/price\")\n"
                "def price(id):\n"
                "    return load(id).price\n"}),
        "storefront": _repo(src, "storefront", {
            "app/product.js":
                "export async function product(id) {\n"
                "  return api.get(`/v1/products/${id}`);\n"
                "}\n"}),
        "payments": _repo(src, "payments", {
            "app/settle.py":
                "def settle(batch):\n"
                "    for p in batch:\n"
                "        producer.send(\"payment.settled.v1\", {\"payment_ref\": p.ref})\n"}),
        "accounts": _repo(src, "accounts", {
            "app/settlements.py":
                "@consumer.subscribe(\"payment.settled.v1\")\n"
                "def on_settled(payload):\n"
                "    book(payload[\"payment_ref\"])\n",
            "README.md": "Acounts service.\n"}),
        "reports": _repo(src, "reports", {
            "migrations/001_audit.sql": "CREATE TABLE audit_log (id int, at timestamp);\n",
            "app/invoices.py":
                "@consumer.subscribe(\"invoice.issued.v1\")\n"
                "def on_invoice(payload):\n"
                "    add_to_totals(payload[\"total_cents\"])\n"}),
        "billing": _repo(src, "billing", {
            "migrations/001_audit.sql": "CREATE TABLE audit_log (id int, actor text);\n"}),
    }
    # Every branch is cut first, at its start time, then merged at its merge
    # time, in time order, so concurrent work really overlaps.
    events = []
    for n, (repo, branch, who, start, end, files) in enumerate(_plan()):
        events.append((_when(*start), 0, n, "start", repo, branch, who, files))
        events.append((_when(*end), 1, n, "merge", repo, branch, who, files))
    pr = 100
    for at, _, _n, what, repo, branch, who, files in sorted(events):
        p = repos[repo]
        if what == "start":
            _git(p, "checkout", "-q", "main", at=at)
            _git(p, "checkout", "-q", "-b", branch, at=at)
            _write(p, files)
            _git(p, "add", "-A", at=at)
            _git(p, "commit", "-q", "-m", f"work on {branch}", at=at, email=who)
            _git(p, "checkout", "-q", "main", at=at)
        else:
            pr += 1
            _git(p, "merge", "-q", "--no-ff", branch, "-m",
                 f"Merge pull request #{pr} from acme/{branch}", at=at)
            _git(p, "branch", "-q", "-D", branch, at=at)
    cfg = root / "ledger.yml"
    cfg.write_text("org: acme\nproject_keys: [ABC]\nintegration_branches: []\n")
    return cfg


def score(replay) -> dict:
    """The replay's findings against the labels."""
    from ledger import replay as rp
    rows = []
    for c in replay.cases:
        lab = LABELS.get(c.change.ref)
        if lab is None:
            continue
        flagged = c.outcome == "context"
        rows.append({"ref": c.change.ref, "expected": lab.strength if lab.flag else None,
                     "got": c.strength if flagged else None, "ticket": lab.ticket,
                     "links": sorted({rp.link_kind(i.get("how")) for i in (c.brief or {}).get("impacts", [])})})
    pos = [r for r in rows if r["expected"]]
    neg = [r for r in rows if not r["expected"]]
    flagged = [r for r in rows if r["got"]]
    no_ticket = [r for r in pos if not LABELS[r["ref"]].ticket]

    def rate(a, b):
        return round(a / b, 2) if b else None

    return {
        "changes": len(rows),
        "recall": rate(sum(bool(r["got"]) for r in pos), len(pos)),
        "precision": rate(sum(bool(r["expected"]) for r in flagged), len(flagged)),
        "recall_without_ticket": rate(sum(bool(r["got"]) for r in no_ticket), len(no_ticket)),
        "false_alarms": sum(bool(r["got"]) for r in neg),
        "strength_right": rate(sum(r["got"] == r["expected"] for r in pos), len(pos)),
        "conflicts_found": f"{sum(r['got'] == 'conflict' for r in pos if r['expected'] == 'conflict')}"
                           f"/{sum(r['expected'] == 'conflict' for r in pos)}",
        "by_strength": {s: sum(r["got"] == s for r in rows) for s in ("conflict", "dependency", "correlation")},
        "seconds": round(replay.seconds, 2),
        "rows": rows,
    }


def measure() -> dict:
    from ledger import replay as rp
    from ledger.adapters.vcs.localgit import LocalGitAdapter
    from ledger.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = build(Path(tmp))
        os.environ.setdefault("LEDGER_CACHE_DIR", "off")
        got = rp.run(Config.load(str(cfg)), LocalGitAdapter(str(Path(tmp) / "src")),
                     since=date.today() - timedelta(days=30), author=None, limit=100)
        return score(got)


if __name__ == "__main__":
    out = measure()
    if "--rows" not in sys.argv:
        out.pop("rows")
    print(json.dumps(out, indent=2))
