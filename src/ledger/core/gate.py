"""Which verdicts stop a merge. None, until somebody says so.

Every surface is advisory by default: a tool that red-builds a branch on its
own outage gets removed, rightly. Enforcement is earned per verdict class --
measure how often a class is right on your own changes, and turn on the ones
that hold.

So a deployment names the classes it enforces, and a gate fails only on those.
`proceed` cannot be enforced; it is the verdict that says nothing was found.
`insufficient_data` can be, by a team that would rather stop than merge blind
-- but it is never implied by enforcing anything else.
"""

from __future__ import annotations

VERDICTS = ("proceed", "review_with", "decision_needed", "insufficient_data")
ENFORCEABLE = ("review_with", "decision_needed", "insufficient_data")


def validate_enforce(raw) -> tuple[str, ...]:
    got = tuple(raw or ())
    bad = [v for v in got if v not in ENFORCEABLE]
    if bad:
        raise ValueError(f"`enforce` can name {', '.join(ENFORCEABLE)}; not {', '.join(map(str, bad))}")
    return got


def blocks(recommendation: str, enforce) -> bool:
    """Does this verdict stop the merge, in a deployment enforcing `enforce`?"""
    return recommendation in tuple(enforce or ())
