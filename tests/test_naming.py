"""Finding contracts in a task description or a plan.

Before a change exists there is no diff, and for most of a session there
never is one. What exists is prose. The rules are the documentation
matcher's, for the same reason: matching contract names literally linked a
table called `invoice` to 147 unrelated pages.
"""

from __future__ import annotations

import pytest

from ledger.core.contracts import ContractRef, Surface
from ledger.core.naming import matchable, named_in

TABLE = ContractRef(Surface.TABLE, "invoice_documents")
ROUTE = ContractRef(Surface.HTTP, "/invoices/{invoice_id}/documents")
TOPIC = ContractRef(Surface.TOPIC, "invoice.events.v1")
ALL = [TABLE, ROUTE, TOPIC]


class TestNamedIn:
    def test_it_finds_what_a_request_names(self):
        text = ("For PLT-321 change `invoice_documents` so the recipient id is required, and make "
                "/invoices/{invoiceId}/documents reject uploads without it.")
        assert named_in(text, ALL) == [TABLE, ROUTE]

    def test_a_route_matches_however_the_placeholder_is_spelt(self):
        assert named_in("call GET /invoices/{id}/documents/ first", [ROUTE]) == [ROUTE]

    def test_a_longer_name_does_not_match_a_shorter_contract(self):
        assert named_in("the invoice_documents_archive table", [ContractRef(Surface.TABLE, "invoice_documents")]) == []

    def test_a_shorter_route_is_not_matched_by_a_longer_one(self):
        assert named_in("POST /invoices/{id}/documents", [ContractRef(Surface.HTTP, "/documents")]) == []

    def test_an_ordinary_word_is_never_a_match(self):
        """`invoice` and `vendor` are real tables. They are also words."""
        for word in ("invoice", "vendor", "document"):
            ref = ContractRef(Surface.TABLE, word)
            assert not matchable(ref)
            assert named_in(f"the {word} is rejected after the final call", [ref]) == []

    def test_a_route_of_placeholders_only_is_not_matchable(self):
        assert not matchable(ContractRef(Surface.HTTP, "/{asset_id}"))

    def test_results_come_in_the_order_the_text_names_them(self):
        text = "publish invoice.events.v1 after writing invoice_documents"
        assert named_in(text, ALL) == [TOPIC, TABLE]

    def test_empty_text_finds_nothing(self):
        assert named_in("", ALL) == [] and named_in(None, ALL) == []


class TestANameAtTheEndOfASentence:
    """A full stop is punctuation, not part of the name. Treating it as part
    of the name made every contract mentioned at the end of a sentence
    invisible to the prompt hook and the plan hook -- found by asking the
    prompt hook about "Also check order_refund_row." and getting nothing,
    while the index knew that table was sitting unreleased on `stage`.
    """

    KNOWN = [ContractRef(Surface.TABLE, "order_refund_row"),
             ContractRef(Surface.TABLE, "invoice_call"),
             ContractRef(Surface.TOPIC, "invoice.events.v1"),
             ContractRef(Surface.TOPIC, "events.v1")]

    def names(self, text):
        return [c.name for c in named_in(text, self.KNOWN)]

    @pytest.mark.parametrize("text", [
        "Also check order_refund_row.",
        "See order_refund_row; it moved.",
        "order_refund_row, and the rest",
        "(order_refund_row)",
        "`order_refund_row`.",
        "it is order_refund_row!",
    ])
    def test_punctuation_after_a_name_is_punctuation(self, text):
        assert self.names(text) == ["order_refund_row"]

    def test_a_dot_followed_by_a_word_is_still_part_of_the_name(self):
        """The rule this boundary exists for: `events.v1` is not a mention of
        the topic `invoice.events.v1`, and the reverse."""
        assert self.names("we publish to invoice.events.v1 now") == ["invoice.events.v1"]

    def test_a_longer_identifier_still_does_not_match(self):
        assert self.names("the order_refund_rows table") == []
