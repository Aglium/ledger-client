"""Which branch is a change actually built on?

Diffing every branch against the default branch is wrong wherever teams work
off an integration branch. A branch cut from `stage` diffed against `main`
drags in everything `stage` holds that `main` does not -- on a real estate,
415 commits and 555 files for a branch that had no commits of its own at all.
Every contract in that diff then gets attributed to the wrong change.

The base is whichever candidate leaves the fewest commits unique to the
branch: that is the branch it was cut from, or the one it has since been
merged into.
"""

from __future__ import annotations


def choose_base(ahead: dict[str, int], default: str) -> tuple[str, int]:
    """Pick the base with the fewest commits unique to the change.

    `ahead` maps candidate base -> commits in the change that are not in that
    base. Ties go to the default branch: the diff is the same size either way,
    and the default branch is what a reviewer would assume.
    """
    if not ahead:
        raise ValueError("no candidate bases to choose from")
    fewest = min(ahead.values())
    if ahead.get(default) == fewest:
        return default, fewest
    for name in sorted(ahead):          # deterministic among equal non-default bases
        if ahead[name] == fewest:
            return name, fewest
    raise AssertionError("unreachable")
