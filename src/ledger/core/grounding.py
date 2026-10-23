"""Every name a model claims must appear literally in the source it cites.

This is the cheapest kill of the largest false-positive class. A strict JSON
schema guarantees the *shape* of an answer and says nothing about its truth: a
model can return a perfectly-shaped manifest asserting a field that exists
nowhere in the diff. Downstream, that invented field becomes a contract, then a
claim, then a conflict alert about something that does not exist -- and the
reviewer who checks once and finds nothing stops checking.

The check is a substring search. That is the entire mechanism, and its
cheapness is the point: it costs microseconds and removes a class of error that
no amount of prompt engineering reliably prevents.

It composes with redaction by design: scrubbing removes *values* and keeps
*keys*, and every claim worth grounding is a key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class GroundingResult:
    grounded: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.grounded) + len(self.ungrounded)

    @property
    def ratio(self) -> float:
        """1.0 when everything claimed was found. Undefined-as-1.0 for no claims:
        an extraction that asserts nothing has invented nothing."""
        return 1.0 if not self.total else len(self.grounded) / self.total

    @property
    def clean(self) -> bool:
        return not self.ungrounded


class Ungrounded(ValueError):
    """Raised in strict mode. Carries the offending names so a retry prompt can
    name them rather than asking vaguely for a better answer."""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"{len(names)} claim(s) absent from the cited source: {', '.join(names[:8])}")


def _variants(name: str) -> tuple[str, ...]:
    """Forms a field legitimately takes across a polyglot estate.

    `orders[].customerId` in a TypeScript client is `customer_id` in the
    Python service and `customerId` in the JSON on the wire. Accepting those
    is not laxity -- they are the same token, spelled per language convention.
    What is NOT accepted is a name that appears in no form at all.
    """
    bare = name.split(".")[-1].replace("[]", "").strip()
    if not bare:
        return ()
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", bare).lower()
    camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), snake)
    return tuple(dict.fromkeys((name, bare, snake, camel, snake.replace("_", ""), bare.lower())))


def verify(claims: list[str], source: str, *, strict: bool = False) -> GroundingResult:
    """Split `claims` into those present in `source` and those invented."""
    res = GroundingResult()
    haystack = source or ""
    lowered = haystack.lower()

    for claim in claims:
        if not claim or not claim.strip():
            continue
        hit = any(
            (v in haystack) or (v.lower() in lowered)
            for v in _variants(claim)
        )
        (res.grounded if hit else res.ungrounded).append(claim)

    if strict and res.ungrounded:
        raise Ungrounded(res.ungrounded)
    return res


def drop_ungrounded(claims: list[str], source: str) -> tuple[list[str], GroundingResult]:
    """Keep only what the source supports.

    The default policy: a manifest with fewer, true claims beats one with more
    claims of unknown truth, because the whole system's value is that an alert
    means something.
    """
    res = verify(claims, source)
    return res.grounded, res
