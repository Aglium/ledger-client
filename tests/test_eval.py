"""The findings eval: anonymized cases rank exactly as the originals, hold no
original name, and a run that gets worse fails."""

from __future__ import annotations

import json

from ledger import anonymize, evaluate

WORDS = anonymize.template_words()


def row(label="useful", **brief):
    b = {"schema": 20, "subject": {"repo": "acme-orders", "ref": "feat/abc-12-refunds",
                                   "tickets": ["ABC-12"], "own_commits": 2},
         "recommendation": "review_with", "confidence": "high",
         "reasons": ["2 change(s) in 1 other repo(s) share this ticket: acme-billing"],
         "not_local": [], "contracts": [], "field_breaks": [], "contract_breaks": [],
         "impacts": [{"repo": "acme-billing", "ref": "ABC-12-refund-ledger", "kind": "branch",
                      "author": "jane.doe@acme.example", "shared_tickets": ["ABC-12"], "sha": "0295f3833b5e",
                      "how": "shares ABC-12, which this diff names and the branch name does not",
                      "relation": None, "by_repository": False, "shares": ["refund_total"], "where": None}],
         "decisions": [{"repo": "acme-orders", "path": "docs/adr/0004-refunds-through-billing.md",
                        "decision_id": "refunds-through-billing:4e0f1db332", "title": "Refunds go through billing",
                        "status": "accepted", "strength": "weak", "basis": "words",
                        "why": "names `refund_total`, a field this change touches", "integrated": False}]}
    b.update(brief)
    return {"n": 1, "label": label, "brief": b, "linked": ["review_with", "high"],
            "quiet": ["proceed", "medium", []]}


def pseudonyms():
    return anonymize.Pseudonyms("test-salt", deny={"acme", "ABC"}, words=WORDS)


class TestAnonymize:
    def test_no_original_name_is_left(self):
        p = pseudonyms()
        text = json.dumps(anonymize.case(row(), p))
        assert anonymize.leaks(text, p) == []
        import re
        tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", text)}
        for name in ("acme", "refund", "billing", "jane", "doe", "0295f3833b5e", "refunds"):
            assert name not in tokens, name

    def test_the_ranking_reads_the_same_case(self):
        r = row()
        c = anonymize.case(r, pseudonyms(), touches={"decision:acme-orders/docs/adr/0004-refunds-through-billing.md": 3})
        orig = {**r, "id": c["id"], "touches": {"decision:acme-orders/docs/adr/0004-refunds-through-billing.md": 3}}
        for promote in (2, 3):
            assert evaluate.run([orig], promote=promote).cases == evaluate.run([c], promote=promote).cases

    def test_ledger_words_stay_and_names_go(self):
        p = pseudonyms()
        how = p.prose("shares ABC-12, which this diff names and the branch name does not")
        assert how.startswith("shares ") and "which this diff names" in how and "ABC" not in how
        path = p.name("docs/adr/0004-x.md")
        assert path.startswith("docs/adr/") and path.endswith(".md") and "0004" not in path

    def test_a_ticket_number_is_hidden_and_still_matches_the_branch(self):
        from ledger.core.rank import _names_key
        p = pseudonyms()
        ticket, branch = p.name("ABC-919"), p.name("feat/abc-919-refunds")
        assert "919" not in ticket and _names_key(branch, ticket)

    def test_the_same_name_is_the_same_pseudonym(self):
        p = pseudonyms()
        assert p.name("refund_total") == p.name("refund_total") != p.name("refund_totals")
        assert len(p.name("refund_total")) == len("refund_total")

    def test_without_a_salt_nothing_is_made(self):
        import pytest
        with pytest.raises(ValueError):
            anonymize.Pseudonyms("")


class TestCompare:
    def test_worse_fails_and_names_the_case(self):
        c = anonymize.case(row(), pseudonyms())
        base = evaluate.run([c], promote=3).to_dict()
        quiet = {**c, "brief": {**c["brief"], "impacts": []}}        # the link is lost
        ok, lines = evaluate.compare(evaluate.run([quiet], promote=3), base, {c["id"]: "useful"})
        assert not ok and any("WORSE" in x for x in lines) and any(c["id"] in x for x in lines)

    def test_the_same_run_passes(self):
        c = anonymize.case(row(), pseudonyms())
        base = evaluate.run([c]).to_dict()
        assert evaluate.compare(evaluate.run([c]), base, {})[0]

    def test_the_cases_hold_to_the_committed_baseline(self):
        from pathlib import Path
        here = Path(__file__).resolve().parents[1] / "eval"
        cases = here / "cases.jsonl"
        base = json.loads((here / "baseline.json").read_text())
        rows = evaluate.read_cases(cases)
        ok, lines = evaluate.compare(evaluate.run(rows, promote=base["promote"]), base,
                                     {r["id"]: r["label"] for r in rows})
        assert ok, "\n".join(lines)
