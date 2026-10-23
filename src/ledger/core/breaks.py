"""Removed fields: the break nobody declares.

`documents[].recipientId` disappears from a payload and a service in another
repo stops working. No OpenAPI file mentions it, no test in this repo fails,
and the reviewer has no way to know. This module finds those removals in a
diff, given what the estate knows other repos read.

Deliberately literal. A field counts as removed when a deleted line names it
and no added line does: renames, reformatting and moves are not breaks, and a
false break here is expensive -- it is exactly the alert people learn to skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ledger.core.contracts import ContractRef, Surface


@dataclass(frozen=True)
class RemovedContract:
    """A whole contract this change stops naming, that another repo uses.

    Deleting the route, topic or table itself is the larger sibling of
    deleting one of its fields, and reads the same way to whoever consumes it.
    """

    contract: ContractRef
    evidence: str
    path: str


@dataclass(frozen=True)
class RemovedField:
    contract: ContractRef
    field_path: str
    #: The deleted line, trimmed. Evidence a human can check in one glance.
    evidence: str
    path: str


#: Prose is not a payload. Deleting a sentence that mentions a field in a
#: design note breaks nothing, and on real history these were most of the hits.
_PROSE = re.compile(r"\.(md|markdown|rst|txt|adoc)$|(^|/)(docs?|adr|decisions)/", re.I)

#: Names shaped like a generic column: `invoice_id`, `created_at`, `id`. On real
#: history these matched SQL rewrites about entirely different tables.
_GENERIC_SHAPE = re.compile(r"(_id|_at|_ts|_date|_by)$|^id$", re.I)

#: Leaf names common enough to appear in almost any deleted line. Measured on
#: real history: `code`, `status`, `payload` and `source` produced a stream of
#: matches that had nothing to do with the contract. A generic name is only
#: believed when the same diff also names the contract or the field's parent.
_GENERIC = frozenset("""
id ids key keys code codes name names type types kind kinds value values data
field fields flag flags error errors status state source target result results
payload body message messages reason reasons path paths url urls total count
sum size date time at created updated deleted start end from to text label
version versions index order position amount currency phone email address
""".split())

#: Comment and docstring lines. A field named in prose is documentation, and on
#: real history these produced hit after hit -- "Returns the task_id, or None".
_COMMENT = re.compile(r'^\s*(#|//|/\*|\*|--|"""|\'\'\'|<!--|>)')


def _leaf(field_path: str) -> str:
    """`documents[].recipientId` -> `recipientId`; the spelling code uses."""
    return re.split(r"[.\[\]]+", field_path.strip())[-1] or field_path


def _is_generic(leaf: str) -> bool:
    """`invoice_id` is a bare key; `matched_recipient_id` names a specific thing.

    Both end in `_id`, so the suffix alone cannot decide. A name of one or two
    words is generic; more than that is specific enough to trust on its own.
    """
    if leaf.lower() in _GENERIC:
        return True
    if not _GENERIC_SHAPE.search(leaf):
        return False
    words = [w for w in re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])", leaf) if w]
    return len(words) <= 2


def _structural(leaf: str, line: str, *, generic: bool = False) -> bool:
    """Does the line use the name as a field, rather than merely contain it?

    `"recipientId": x`, `doc.recipientId`, `payload["recipientId"]` are field
    references. `for invoice_id in invoice_ids:` is a loop variable, and on real
    history that shape was a steady source of false breaks.
    """
    e = re.escape(leaf)
    shapes = [rf"""["'`]{e}["'`]""", rf"\.{e}\b"]      # a quoted key, an attribute
    if not generic:
        # A bare `code = ...` is a local variable far more often than a field;
        # measured, that shape was the last false break left on real history.
        shapes.append(rf"(?<![\w.]){e}\s*[:=]")
    return any(re.search(s, line) for s in shapes)


def _file_is_about(contract: ContractRef, path: str, file_text: dict[str, str], body: str) -> bool:
    """Does this file belong to the contract at all?

    A distinctive name (`apex.document.results.v1`, `invoice_price`) must
    appear in the file or the diff. An ordinary word (`invoice`) can never
    establish that, so a contract named like one is skipped here rather than
    believed: its fields are matched only through a more specific signal.
    """
    if not _DISTINCTIVE_CONTRACT.search(contract.name) and len(contract.name) < 12:
        return False
    haystack = file_text.get(path, "") + "\n" + body
    return contract.name in haystack


def _corroborated(contract: ContractRef, field_path: str, body: str) -> bool:
    """Does this diff name the contract, or the field's parent path?

    `status` alone means nothing. `status` in a hunk that also names
    `apex.document.results.v1`, or `documents[]`, is about that contract.
    """
    if contract.name and contract.name in body:
        return True
    parents = [seg for seg in re.split(r"[.\[\]]+", field_path) if seg][:-1]
    return any(len(seg) > 3 and re.search(rf"(?<![\w.]){re.escape(seg)}\b", body) for seg in parents)


#: A contract whose name is an ordinary word (`invoice`, `vendor`) says nothing
#: about which code belongs to it. Measured: `table invoice` matched a Reviews
#: refactor because the file, like every file in that repo, says "invoice".
_DISTINCTIVE_CONTRACT = re.compile(r"[./_\-]")


def removed_fields(
    diffs: dict[str, str],
    wanted: dict[ContractRef, list[str]],
    *,
    file_text: dict[str, str] | None = None,
) -> list[RemovedField]:
    """Fields in `wanted` that this diff deletes and does not add back.

    `diffs` maps path -> the diff body for that file (+/- lines). `file_text`,
    when given, maps path -> the whole file, and a field is only reported in a
    file that names its contract. Without that constraint this matched any
    identifier anywhere: on 1,200 real commits it flagged SQL rewrites about
    other tables, PHP property assignments and logger refactors.
    """
    out: list[RemovedField] = []
    seen: set[tuple[str, str]] = set()
    for path, body in diffs.items():
        if _PROSE.search(path):
            continue
        removed = [l[1:] for l in body.splitlines() if l.startswith("-")]
        added = "\n".join(l[1:] for l in body.splitlines() if l.startswith("+"))
        if not removed:
            continue
        for contract, fields in wanted.items():
            if file_text is not None and not _file_is_about(contract, path, file_text, body):
                continue
            for field_path in fields:
                leaf = _leaf(field_path)
                if not leaf or len(leaf) < 3:
                    continue
                # Word boundary: `id` must not match `identity`, and a field
                # still present in the added lines was moved, not dropped.
                word = re.compile(rf"(?<!\w){re.escape(leaf)}\b")
                if word.search(added):
                    continue
                hit = next((l for l in removed
                            if word.search(l)
                            and not _COMMENT.match(l)
                            and _structural(leaf, l, generic=_is_generic(leaf))
                            and (not _is_generic(leaf)
                                 or _corroborated(contract, field_path, l))), None)
                if hit is None:
                    continue
                key = (contract.name, field_path)
                if key in seen:
                    continue
                seen.add(key)
                out.append(RemovedField(contract=contract, field_path=field_path,
                                        evidence=hit.strip()[:160], path=path))
    return sorted(out, key=lambda r: (r.contract.surface.value, r.contract.name, r.field_path))


#: Test code is not the contract. The last false hit on real history was a
#: deleted test-teardown line -- `DROP TABLE IF EXISTS audit_log` -- which is
#: the removal of a DROP, the opposite of dropping the table.
_TEST_PATH = re.compile(r"(^|/)(tests?|spec|conftest)(/|\.|_)|(^|/)test_|_test\.[a-z]+$", re.I)

#: What a contract's *definition* looks like per surface. A mention is not a
#: definition: on real history, matching any deleted mention of a table flagged
#: reverts and test refactors 37 times. Deleting the thing itself is rarer and
#: looks different -- a DROP TABLE, a route decorator, a subscription.
_DEFINITION = {
    # A removed CREATE TABLE is a migration being squashed, not a table being
    # dropped -- that alone accounted for the last false hits on real history.
    Surface.TABLE: re.compile(r"(?i)\bdrop\s+table\b|__tablename__"),
    Surface.HTTP: re.compile(r"(?i)@\w+\.(get|post|put|patch|delete)\s*\(|\b(add_)?(api_)?route\s*\(|"
                             r"\b(app|router|blueprint)\.(get|post|put|patch|delete)\s*\(|path\s*="),
    # No word boundaries: topic definitions read `_topic("KAFKA_TOPIC_X", "a.b.v1")`
    # and `INVOICE_EVENTS_TOPIC = ...`, where "topic" is glued to other words. A
    # removed line that names the topic AND says topic/queue/publish is specific.
    Surface.TOPIC: re.compile(r"(?i)(subscri|consum|publish|produc|queue|exchange|routing_key|topic|listen)"),
}


def removed_contracts(
    diffs: dict[str, str], owned: dict[ContractRef, list[str]], file_text: dict[str, str],
) -> list[RemovedContract]:
    """Contracts this change deletes from a file, that other repos also touch.

    Two guards keep it quiet. The name must be gone from the file after the
    change -- a refactor that moves a topic constant around still leaves it
    there. And the deleted line must look like the contract's definition, not
    a passing mention: matching mentions flagged 37 reverts and test refactors
    over 1,600 real commits.
    """
    out: list[RemovedContract] = []
    for path, body in diffs.items():
        if _PROSE.search(path) or _TEST_PATH.search(path):
            continue
        removed = [l[1:] for l in body.splitlines() if l.startswith("-")]
        added = "\n".join(l[1:] for l in body.splitlines() if l.startswith("+"))
        for contract, others in owned.items():
            if not others:
                continue
            name = contract.name
            # An ordinary word cannot establish that a file is about a contract,
            # and cannot establish that removing the word removes the contract.
            if not _DISTINCTIVE_CONTRACT.search(name) and len(name) < 12:
                continue
            if name in added or name in file_text.get(path, ""):
                continue                    # still there afterwards: not a removal
            pattern = _DEFINITION.get(contract.surface)
            hit = next((l for l in removed
                        if name in l and not _COMMENT.match(l)
                        and (pattern is None or pattern.search(l))), None)
            if hit is None:
                continue
            out.append(RemovedContract(contract=contract, evidence=hit.strip()[:160], path=path))
    return sorted(out, key=lambda r: (r.contract.surface.value, r.contract.name))


def moved_binding(evidence: str, name: str, bound_after) -> bool:
    """Was a removed topic definition moved rather than removed?

    `INVOICE_EVENTS_TOPIC = "invoice.events.v1"` deleted from two modules while the
    same constant stays bound to the same topic in a third is a refactor, not a
    break -- the one false removal left on real history. `bound_after(ident)`
    returns the values that identifier is bound to after the change. A rename
    (the identifier now bound to another topic) is still a removal.
    """
    from ledger.core.bindings import bindings_in

    defined = [b for b in bindings_in("removed-line", evidence) if b.value == name]
    # Judge by the identifier the line DEFINES -- its left-hand side. In
    # `INVOICE_EVENTS_TOPIC = _topic("KAFKA_TOPIC_X", "a.v1")` that is the
    # constant, not the env var: an example env file still saying `a.v1` does
    # not undo a rename of the constant's default.
    lhs = re.match(r"""^\s*(?:export\s+|const\s+|let\s+|var\s+)?['"]?([A-Za-z_][\w.\-]*)['"]?\s*(?::[^=]*)?[=:]""",
                   evidence)
    if lhs:
        defined = [b for b in defined if b.raw == lhs.group(1)] or defined
    return any(name in bound_after(b.raw) for b in defined)
