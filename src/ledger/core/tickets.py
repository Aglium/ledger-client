"""Ticket-key extraction from VCS text (branch names, PR titles, commit subjects).

No vendor or org vocabulary lives here: the project keys are supplied by the
caller from config, so `ABC` below is an example, not a built-in.

Why this exists: where branch naming is disciplined
(`{type}/{KEY}-{n}-{description}`), ticket ids already link cross-repo work. Joining on that costs one
regex and has near-zero false positives, which is why it ships before anything
inference-based.
"""

from __future__ import annotations

import re
from functools import lru_cache

# A key is letters (>=2). A ticket is KEY-digits. We require the match to be
# bounded by a non-alphanumeric on both sides so `ABC-1237` never yields
# `ABC-123`, and `xabc-123` never yields `ABC-123`.
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,19}$")


@lru_cache(maxsize=64)
def _pattern(keys: tuple[str, ...]) -> re.Pattern[str]:
    alt = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])({alt})[-_]?(\d+)(?![A-Za-z0-9])", re.IGNORECASE)


def validate_keys(keys: list[str]) -> tuple[str, ...]:
    """Reject malformed project keys loudly at config load, not at match time."""
    out: list[str] = []
    for k in keys:
        k = k.strip()
        if not _KEY_RE.match(k):
            raise ValueError(f"invalid project key {k!r}: expected 2-20 alphanumerics starting with a letter")
        out.append(k.upper())
    if not out:
        raise ValueError("no project keys configured: the ticket join cannot run")
    return tuple(dict.fromkeys(out))


def extract_tickets(text: str | None, keys: tuple[str, ...]) -> list[str]:
    """Return normalised ticket keys (`ABC-123`) found in `text`, order-preserved, deduped.

    Case-insensitive: branches tend to spell it `abc-123`, commit subjects `ABC-123`.

    Number zero is not a ticket. No tracker issues from zero, and a team that
    needs a key for work that has none writes one: on the estate this was
    measured against, `SMT-0` is a house convention --
    `SMT-0-remove-old-configuration`, `SMT-0 Fixed merge conflicts` -- and it
    appeared on 94 open changes across 8 repositories. Joining on it links
    everything that has no ticket to everything else that has no ticket, which
    is the worst possible answer: confidently wrong rather than silent.
    """
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _pattern(keys).finditer(text):
        number = int(m.group(2))
        if number == 0:
            continue
        seen.setdefault(f"{m.group(1).upper()}-{number}", None)
    return list(seen)
