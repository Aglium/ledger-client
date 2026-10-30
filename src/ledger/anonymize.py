"""Turn judged briefs into eval cases that hold no company's names.

A case is what the ranking reads, and only that: the links, their kinds and
flags, the contracts' directions, the decisions' basis, the verdict the links
gave, the label a person or session gave it, and the model's 0-3 per item.
Everything a reader could learn the company from is gone or replaced:

  * every name -- repository, branch, topic, table, route, path, field, sha,
    author, ticket key and number, date -- becomes a keyed pseudonym of the same length, the
    same one everywhere it occurs, so "both diffs name `x`" still holds;
  * the sentences Ledger itself wrote keep the words of its own templates,
    which the ranking matches on ("which this diff names", "same author");
    any other word in them is replaced like a name;
  * text nobody ranks on -- titles, effects, asks, open conditions, reasons --
    is dropped, or kept as an opaque tag where equality is all that matters.

The key is a secret the person exporting holds (`LEDGER_EVAL_SALT`); without
it a pseudonym cannot be reversed by guessing names and hashing them.
`leaks()` lists any original name still present, and export refuses to write
while there is one.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import re
from pathlib import Path

#: Words a path, branch or author name keeps, because a rule reads them: test
#: and documentation folders, integration branches, bot accounts, file kinds.
STRUCTURAL = frozenset("""
docs doc adr adrs decisions rfc rfcs test tests spec specs e2e __tests__ tests_functional
migrations migration versions vendor node_modules src app lib stage staging develop master main
release feat feature fix bugfix hotfix chore refactor bot ci cd jenkins renovate dependabot github
actions gitlab runner automation svc service account md rst txt py php ts tsx js jsx go rb java kt sql
yml yaml json proto graphql toml lock d v1 v2 v3 v4 openapi swagger
""".split())

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_HEX = re.compile(r"^(?=.*[0-9])(?=.*[a-f])[0-9a-f]{7,}$")
_TICK = re.compile(r"`([^`]*)`")
_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def template_words() -> frozenset[str]:
    """Words in the string literals of the client's own code: its templates."""
    import ledger
    out: set[str] = set()
    for path in Path(ledger.__file__).parent.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.update(w.lower() for w in _TOKEN.findall(node.value) if w.isalpha())
    return frozenset(out)


class Pseudonyms:
    def __init__(self, salt: str, *, deny: set[str] = frozenset(), words: frozenset[str] | None = None):
        if not salt:
            raise ValueError("a secret salt is required (LEDGER_EVAL_SALT)")
        self.key = salt.encode()
        self.words = (words if words is not None else template_words()) - {d.lower() for d in deny}
        self.deny = {d.lower() for d in deny}
        self.seen: set[str] = set()         # original tokens replaced, for the leak check
        self.made: set[str] = set()         # pseudonyms written; one may equal another original

    def _code(self, low: str) -> str:
        digest = hmac.new(self.key, low.encode(), hashlib.sha256).digest()
        if _HEX.match(low):
            return digest.hex()[:len(low)]
        return "".join(_ALPHA[b % 26] for b in digest)[:len(low)] if len(low) <= 32 else \
            "".join(_ALPHA[b % 26] for b in (digest * 4))[:len(low)]

    def token(self, tok: str, *, keep: frozenset[str]) -> str:
        low = tok.lower()
        if low in keep and low not in self.deny:
            return tok
        if tok.isdigit():
            # Ticket numbers, dates, ADR numbers: digits of the same length, so
            # `SK-919` in a branch name still matches `SK-919` in a ticket.
            self.seen.add(low)
            digest = hmac.new(self.key, b"#" + low.encode(), hashlib.sha256).digest()
            out = "".join(str(b % 10) for b in digest)[:len(tok)].rjust(len(tok), "0")
            self.made.add(out)
            return out
        self.seen.add(low)
        code = self._code(low)
        self.made.add(code)
        if tok.isupper():
            return code.upper()
        if tok[:1].isupper():
            return code.capitalize()
        return code

    def name(self, s: str | None) -> str | None:
        """A name: every token but the structural ones is replaced."""
        if s is None:
            return None
        return _TOKEN.sub(lambda m: self.token(m.group(0), keep=STRUCTURAL), s)

    def prose(self, s: str | None) -> str | None:
        """Ledger's sentence: its own words stay, names in backticks do not."""
        if s is None:
            return None
        parts = []
        last = 0
        for m in _TICK.finditer(s):
            parts.append(self._words(s[last:m.start()]))
            parts.append("`" + self.name(m.group(1)) + "`")
            last = m.end()
        parts.append(self._words(s[last:]))
        return "".join(parts)

    def _words(self, s: str) -> str:
        return _TOKEN.sub(lambda m: self.token(m.group(0), keep=self.words | STRUCTURAL), s)

    def tag(self, s: str) -> str:
        """Opaque text where only equality matters."""
        return "text:" + hmac.new(self.key, s.encode(), hashlib.sha256).hexdigest()[:12]


def _ticket(p: Pseudonyms, t: str) -> str:
    return p.name(t)


def case(row: dict, p: Pseudonyms, *, touches: dict[str, int] | None = None) -> dict:
    """One judged brief as an eval case. `row` holds the brief and its label."""
    from ledger.core.brief import NOTHING_LEADS
    note = NOTHING_LEADS.split("{n}")[0]
    b = row["brief"]

    def reason(r: str) -> str:
        return p.prose(r) if r.startswith(note) else p.tag(r)

    subject = b.get("subject") or {}
    out = {
        "schema": b.get("schema"),
        "subject": {"repo": p.name(subject.get("repo")), "ref": p.name(subject.get("ref")),
                    "tickets": [_ticket(p, t) for t in subject.get("tickets") or []],
                    "own_commits": subject.get("own_commits")},
        "recommendation": b["recommendation"], "confidence": b["confidence"],
        "reasons": [reason(r) for r in b.get("reasons") or []],
        "not_local": [p.prose(x) for x in b.get("not_local") or []],
        "contracts": [{"surface": c["surface"], "name": p.name(c["name"]),
                       "also_touched_by": [p.name(x) for x in c.get("also_touched_by") or []],
                       "parties": [{"repo": p.name(x.get("repo")), "uses": x.get("uses")}
                                   for x in c.get("parties") or []],
                       "fields": [{"direction": f.get("direction")} for f in c.get("fields") or []]}
                      for c in b.get("contracts") or []],
        "field_breaks": [{"surface": f["surface"], "name": p.name(f["name"]),
                          "field_path": p.name(f["field_path"])} for f in b.get("field_breaks") or []],
        "contract_breaks": [{"surface": c["surface"], "name": p.name(c["name"])}
                            for c in b.get("contract_breaks") or []],
        "impacts": [{"repo": p.name(i["repo"]), "ref": p.name(i["ref"]), "kind": i.get("kind"),
                     "author": p.name(i.get("author")),
                     "shared_tickets": [_ticket(p, t) for t in i.get("shared_tickets") or []],
                     "where": p.name(i.get("where")), "sha": p.name(i.get("sha")),
                     "how": p.prose(i.get("how")), "relation": p.prose(i.get("relation")),
                     "by_repository": i.get("by_repository"),
                     "shares": None if i.get("shares") is None else [p.name(x) for x in i["shares"]]}
                    for i in b.get("impacts") or []],
        "decisions": [{"repo": p.name(d["repo"]), "path": p.name(d["path"]),
                       "decision_id": p.name(d.get("decision_id")), "status": d.get("status"),
                       "strength": d.get("strength"), "basis": d.get("basis"),
                       "why": p.prose(d.get("why")), "gate_basis": p.prose(d.get("gate_basis")),
                       "integrated": d.get("integrated"), "ref": p.name(d.get("ref"))}
                      for d in b.get("decisions") or []],
    }
    linked = row.get("linked")
    quiet = row.get("quiet")
    # Item keys are rebuilt from the brief, so the model's scores follow the
    # items in order rather than by re-deriving each key's text.
    from ledger.core.rank import items_of
    before = [it.key for it in items_of(b)]
    after = [it.key for it in items_of(out)]
    renamed = dict(zip(before, after)) if len(before) == len(after) else {}
    return {
        "id": p.tag(f"{subject.get('repo')}:{subject.get('ref')}:{row.get('n')}")[5:],
        "label": row["label"],
        "brief": out,
        "linked": list(linked) if linked else None,
        "quiet": [quiet[0], quiet[1], [reason(r) for r in quiet[2]]] if quiet else None,
        "touches": {renamed[k]: v for k, v in (touches or {}).items() if k in renamed},
    }


def leaks(text: str, p: Pseudonyms) -> list[str]:
    """Original names still present in `text`, as whole tokens."""
    present = {t.lower() for t in _TOKEN.findall(text)}
    keep = p.words | STRUCTURAL
    # A token both replaced and written as some other name's pseudonym is a
    # collision, not a leak: it says nothing about the name it came from.
    return sorted(t for t in (p.seen & present) - p.made if t not in keep and len(t) > 2)
