"""Finding known contracts in free text -- a task description, a plan.

Before a change exists there is no diff to read, and for most of a session
that is the situation: the task has been stated, files are being read, a plan
is being written. What exists in that phase is prose, and prose names things.

The same two rules the documentation matcher earns its precision with apply,
for the same reason -- literal matching linked a table called `invoice` to 147
unrelated wiki pages:

  separated   a name with no separator (`invoice`, `vendor`) is an English
              word before it is an identifier, and is never matched.
  bounded     `invoice_document` does not match inside `invoice_documents`, and
              `/invoices` does not match inside `/invoices/{id}/documents`.

A dot is part of a name only when a word follows it. `invoice` must not match
inside `invoice.events.v1`, but a table named at the end of a sentence --
"Also check order_refund_row." -- has a full stop after it, and treating
that as part of the name made every such mention invisible. Prose ends
sentences; this is not an edge case.

Routes are additionally matched on normalised segments, so a plan writing
`/invoices/{invoiceId}` finds the route defined as `/invoices/{invoice_id}`.
"""

from __future__ import annotations

import re

from ledger.core.contracts import ContractRef, Surface, normalize_http

_SEPARATED = re.compile(r"[._/-]")
#: Matches how a plan or a ticket writes a path: `/invoices/{invoiceId}/documents`.
_ROUTE = re.compile(r"/[\w./{}-]*[\w}]")
_MIN = 6


def matchable(ref: ContractRef) -> bool:
    """Is this contract's name distinctive enough to look for in prose?"""
    if ref.surface == Surface.HTTP:
        segs = normalize_http(ref.name)
        return bool(segs) and any(s != "{}" for s in segs)
    return len(ref.name) >= _MIN and bool(_SEPARATED.search(ref.name))


def named_in(text: str, contracts) -> list[ContractRef]:
    """Which of `contracts` this text names, in the order they first appear."""
    if not text:
        return []
    low = text.lower()
    # Where each route shape first appears, so the answer can be ordered by
    # the text: the contract's own spelling may not occur in it at all.
    routes: dict[tuple, int] = {}
    for m in _ROUTE.finditer(text):
        routes.setdefault(normalize_http(m.group()), m.start())
    found: dict[tuple, tuple[int, ContractRef]] = {}
    for ref in contracts:
        if not matchable(ref):
            continue
        key = (ref.surface.value, ref.name)
        if key in found:
            continue
        if ref.surface == Surface.HTTP:
            at = routes.get(normalize_http(ref.name))
            if at is not None:
                found[key] = (at, ref)
            continue
        if ref.name.lower() not in low:            # cheap reject before the regex
            continue
        m = re.search(rf"(?<![\w/-])(?<!\w\.){re.escape(ref.name)}(?![\w/-])(?!\.\w)",
                      text, re.I)
        if m:
            found[key] = (m.start(), ref)
    return [ref for _, ref in sorted(found.values(), key=lambda p: p[0])]
