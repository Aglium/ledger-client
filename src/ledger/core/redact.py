"""Strip personal data from source before it leaves the network.

Why this is not optional: test fixtures and seed data routinely carry real
emails, IBANs, phone numbers and dates of birth, and they are concentrated in
exactly the files that appear in diffs. A diff is the unit this system sends to
a model vendor, so without this, customers' personal data leaves the network.

The design turns on one observation: **contract extraction needs keys and types,
not values.** "what fields does this payload carry" is answerable from
`{"email": "<redacted:email>"}` exactly as well as from the real address. So
redacting values while preserving structure is both safer and *better* for the
task, rather than a quality trade.

Two layers, in order:

  1. key-aware   `"email": "a@b.c"` -> `"email": "<redacted:email>"`
                 Precise, structure-preserving, handles the fixture case.
  2. pattern     free-text emails/IBANs/phones/long digit runs anywhere else.
                 Coarse, and deliberately over-eager: a false positive costs a
                 slightly worse manifest, a false negative costs a data leak.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

#: Key names whose VALUE is personal. Matched case-insensitively against the
#: whole key, and against snake/camel/kebab variants.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "email", "emailaddress", "email_address", "mail",
    "phone", "phonenumber", "phone_number", "mobile", "tel", "telephone", "msisdn",
    "iban", "bic", "swift", "accountnumber", "account_number", "cardnumber", "card_number",
    "pan", "cvv", "sortcode", "sort_code",
    "firstname", "first_name", "lastname", "last_name", "surname", "givenname", "given_name",
    "middlename", "middle_name", "fullname", "full_name", "name", "signature",
    "address", "addressline1", "address_line_1", "addressline2", "address_line_2",
    "street", "city", "postcode", "postalcode", "postal_code", "zip",
    "dob", "dateofbirth", "date_of_birth", "birthdate", "birth_date", "birthday",
    "passport", "passportnumber", "passport_number", "nationalid", "national_id",
    "ssn", "personalcode", "personal_code", "taxid", "tax_id",
    "pnr", "bookingreference", "booking_reference", "ticketnumber", "ticket_number",
    "ip", "ipaddress", "ip_address", "useragent", "user_agent",
})

TOKEN = "<redacted:{kind}>"

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{10,28}\b")
_PHONE = re.compile(r"(?<![\w.])\+[0-9][0-9\-\s()]{7,17}[0-9](?![\w.])")
_LONG_DIGITS = re.compile(r"(?<![\w.])[0-9]{13,19}(?![\w.])")

# `"key": "value"` / `'key': 'value'` / `key="value"` / `key: value` in YAML.
_KV = re.compile(
    r"""(?P<prefix>["']?(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)["']?\s*[:=]\s*)"""
    # `[^\\]`, not `.`: with `.` a backslash could be read either as the start
    # of an escape or as an ordinary character, and a long value full of
    # escapes with no closing quote in reach -- JSON embedded in JSON, which
    # is every n8n workflow file -- was retried every possible way. One such
    # line of 9,944 characters held a sync for an hour at 100% CPU.
    r"""(?P<quote>["'])(?P<value>(?:\\.|(?!\3)[^\\])*)(?P=quote)""",
)


def _normalise_key(key: str) -> str:
    return re.sub(r"[_\-]", "", key).lower()


def is_sensitive_key(key: str) -> bool:
    return _normalise_key(key) in {_normalise_key(k) for k in SENSITIVE_KEYS}


@dataclass
class Redaction:
    """What was removed. Auditable on purpose: a scrubber nobody can inspect is
    a scrubber nobody should trust."""

    text: str
    counts: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def clean(self) -> bool:
        return self.total == 0

    def summary(self) -> str:
        if self.clean:
            return "no personal data detected"
        return ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))


def redact(text: str) -> Redaction:
    """Return `text` with personal data replaced, plus a report of what went."""
    if not text:
        return Redaction(text=text or "")
    counts: Counter = Counter()

    def _kv(m: re.Match[str]) -> str:
        key, value = m.group("key"), m.group("value")
        if not value or not is_sensitive_key(key):
            return m.group(0)
        counts[_normalise_key(key)] += 1
        q = m.group("quote")
        return f"{m.group('prefix')}{q}{TOKEN.format(kind=_normalise_key(key))}{q}"

    out = _KV.sub(_kv, text)

    for kind, rx in (("email", _EMAIL), ("iban", _IBAN), ("phone", _PHONE), ("digits", _LONG_DIGITS)):
        def _sub(m: re.Match[str], _kind: str = kind) -> str:
            counts[_kind] += 1
            return TOKEN.format(kind=_kind)
        out = rx.sub(_sub, out)

    return Redaction(text=out, counts=counts)


def assert_no_pii(text: str) -> None:
    """Belt and braces for the egress boundary.

    Raises if anything obviously personal survived. Used as a post-condition, so
    a bug in `redact` fails loudly here rather than silently shipping a leak.
    """
    for kind, rx in (("email", _EMAIL), ("iban", _IBAN), ("phone", _PHONE), ("digits", _LONG_DIGITS)):
        m = rx.search(text)
        if m:
            raise ValueError(f"redaction post-condition failed: {kind}-shaped value survived at offset {m.start()}")
