"""Egress tests. The important ones assert what NEVER leaves the machine."""

from __future__ import annotations

import pytest

from ledger.core.redact import Redaction, assert_no_pii, is_sensitive_key, redact

# Synthetic, never real. Shaped like what the fixture audit actually found.
FIXTURE = '''
def test_order_intake():
    payload = {
        "email": "jane.doe@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "1984-03-11",
        "iban": "LT121000011101001000",
        "phone": "+37060012345",
        "booking_reference": "ABC123",
        "documents": [{"customerId": "p-1", "type": "passport"}],
    }
    assert bind_order_to_customer(payload) is not None
'''


class TestRedact:
    def test_removes_every_sensitive_value(self):
        r = redact(FIXTURE)
        for leaked in ("jane.doe@example.com", "Jane", "Doe", "1984-03-11",
                       "LT121000011101001000", "+37060012345", "ABC123"):
            assert leaked not in r.text, f"{leaked!r} survived redaction"

    def test_preserves_the_structure_extraction_actually_needs(self):
        """Keys and code are the signal; values are the liability."""
        r = redact(FIXTURE)
        for kept in ("email", "date_of_birth", "documents", "customerId",
                     "bind_order_to_customer", "def test_order_intake"):
            assert kept in r.text, f"{kept!r} should have survived"

    def test_reports_what_it_removed(self):
        r = redact(FIXTURE)
        assert not r.clean and r.total >= 7
        assert "email" in r.summary()

    def test_free_text_pii_outside_key_value_pairs(self):
        r = redact("contact ops@example.com or +37060012345, iban LT121000011101001000")
        assert "@" not in r.text and "+3706" not in r.text and "LT12" not in r.text

    def test_clean_input_is_untouched(self):
        code = "def bind_order_to_customer(doc, customer_id):\n    return doc\n"
        r = redact(code)
        assert r.text == code and r.clean

    def test_empty_and_none(self):
        assert redact("").clean and redact(None).text == ""

    @pytest.mark.parametrize("key,expected", [
        ("email", True), ("Email", True), ("EMAIL_ADDRESS", True), ("emailAddress", True),
        ("first_name", True), ("firstName", True), ("date-of-birth", True),
        ("customerId", False), ("invoice_id", False), ("status", False), ("amount", False),
    ])
    def test_key_sensitivity(self, key, expected):
        assert is_sensitive_key(key) is expected

    def test_post_condition_catches_a_leak(self):
        with pytest.raises(ValueError, match="post-condition failed"):
            assert_no_pii("oops jane@example.com")
        assert_no_pii("nothing personal here")


def test_a_long_value_full_of_escapes_does_not_hang():
    """JSON embedded in JSON, with no closing quote in reach: every n8n
    workflow file. The pattern could read each backslash two ways and tried
    them all; one 9,944-character line held a sync for an hour."""
    import time
    from ledger.core.redact import redact
    started = time.monotonic()
    redact('"jsCode": "' + '\\\\"a' * 6000)
    redact('"jsCode": "' + '\\"a\\n' * 6000 + '" , "next": "x"')
    assert time.monotonic() - started < 2


def test_an_escaped_quote_inside_a_value_is_still_one_value():
    """The same answer as before the pattern was made unambiguous."""
    from ledger.core.redact import redact
    plain = redact('"email": "ann@example.com", "other": "kept"').text
    escaped = redact('"email": "ann\\"x@example.com", "other": "kept"').text
    assert "example.com" not in plain and "example.com" not in escaped
    assert "kept" in plain and "kept" in escaped
