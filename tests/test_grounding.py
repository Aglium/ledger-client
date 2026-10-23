import pytest

from ledger.core.grounding import Ungrounded, drop_ungrounded, verify

DIFF = '''
+class OrderIntake(BaseModel):
+    customer_id: str
+    booking_reference: str
+    documents: list[Document]
+
+def bind_order_to_customer(doc, customer_id):
+    return doc
'''
TS_DIFF = '+export interface Invoice { customerId: string; bookingReference: string }'


class TestVerify:
    def test_present_names_are_grounded(self):
        r = verify(["customer_id", "booking_reference", "bind_order_to_customer"], DIFF)
        assert r.clean and r.ratio == 1.0

    def test_invented_names_are_caught(self):
        """The whole point: a schema-valid answer can still be fiction."""
        r = verify(["customer_id", "totally_invented_field"], DIFF)
        assert r.grounded == ["customer_id"]
        assert r.ungrounded == ["totally_invented_field"]
        assert r.ratio == 0.5

    def test_case_and_naming_convention_variants(self):
        """customerId in TS is customer_id in Python. Same token, different
        house style -- accepting that is not laxity."""
        assert verify(["customer_id"], TS_DIFF).clean
        assert verify(["customerId"], DIFF).clean

    def test_json_path_notation_resolves_to_the_leaf(self):
        assert verify(["orders[].customerId"], TS_DIFF).clean

    def test_strict_mode_raises_and_names_the_offenders(self):
        with pytest.raises(Ungrounded) as e:
            verify(["customer_id", "ghost_field"], DIFF, strict=True)
        assert e.value.names == ["ghost_field"]
        assert "ghost_field" in str(e.value)

    def test_no_invoices_is_clean_not_an_error(self):
        r = verify([], DIFF)
        assert r.clean and r.ratio == 1.0 and r.total == 0

    def test_blank_invoices_are_ignored(self):
        assert verify(["", "   ", "customer_id"], DIFF).total == 1

    def test_empty_source_grounds_nothing(self):
        r = verify(["customer_id"], "")
        assert r.ungrounded == ["customer_id"]


class TestDropUngrounded:
    def test_keeps_only_what_the_source_supports(self):
        kept, res = drop_ungrounded(["customer_id", "ghost", "documents"], DIFF)
        assert kept == ["customer_id", "documents"]
        assert res.ungrounded == ["ghost"]

    def test_fewer_true_invoices_beat_more_uncertain_ones(self):
        kept, res = drop_ungrounded(["ghost_a", "ghost_b"], DIFF)
        assert kept == [] and res.ratio == 0.0


class TestComposesWithRedaction:
    def test_grounding_still_works_after_pii_is_stripped(self):
        """Redaction removes values and keeps keys; every claim worth grounding
        is a key. The two features are designed to compose."""
        from ledger.core.redact import redact
        fixture = '{"email": "jane@example.com", "customer_id": "p-1", "iban": "LT121000011101001000"}'
        scrubbed = redact(fixture).text
        assert verify(["email", "customer_id", "iban"], scrubbed).clean
        assert "jane@example.com" not in scrubbed

    def test_a_invoice_about_a_redacted_VALUE_is_correctly_ungrounded(self):
        from ledger.core.redact import redact
        scrubbed = redact('{"email": "jane@example.com"}').text
        assert verify(["jane@example.com"], scrubbed).ungrounded == ["jane@example.com"]
