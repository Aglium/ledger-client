"""What the check run says, and why it almost never says "failure".

The rule this module exists to enforce: **no data means `neutral`.** Reporting
`success` when the index has not seen a PR is false confidence -- the reviewer
reads a green tick as "checked, nothing there" when the truth is "not checked".
Reporting `failure` is worse: it blocks the whole org on our own outage, and a
tool that red-builds someone's branch because its database was down gets removed.

`failure` has exactly one way in: a deployment has named a verdict class in its
config's `enforce` list, and the brief for this change reached that verdict.
Nothing is enforced by default, and nothing this tool fails to know -- a change
it has not seen, a scan that was partial -- can produce it, unless a team has
said in so many words that it would rather stop than merge blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ledger.core.join import JoinResult


class Conclusion(str, Enum):
    SUCCESS = "success"
    NEUTRAL = "neutral"
    FAILURE = "failure"      # only for a verdict the deployment enforces


@dataclass(frozen=True)
class CheckVerdict:
    conclusion: Conclusion
    title: str
    summary: str

    @property
    def blocks_merge(self) -> bool:
        return self.conclusion is Conclusion.FAILURE


NAME = "ledger/siblings"


def decide(result: JoinResult | None, *, enforced: tuple[str, list[str]] | None = None) -> CheckVerdict:
    """Map a join result onto a check conclusion.

    `result is None` means the change is not in the index at all, which is a
    different answer from "indexed, nothing found" and must not collapse into it.

    `enforced` is a brief's (recommendation, reasons) when -- and only when --
    this deployment enforces that recommendation. It is the single source of a
    failing check.
    """
    if enforced is not None:
        recommendation, reasons = enforced
        return CheckVerdict(
            Conclusion.FAILURE,
            f"{recommendation}: enforced by this deployment",
            "This deployment's config stops a merge on this verdict. "
            + " ".join(f"- {r}" for r in reasons[:5]),
        )
    if result is None:
        return CheckVerdict(
            Conclusion.NEUTRAL,
            "Not analyzed",
            "This change is not in the index yet, so no claim is made either way. "
            "It is not evidence that nothing else is in flight.",
        )

    if result.partial:
        missing = ", ".join(result.missing_sources) or "one or more sources"
        return CheckVerdict(
            Conclusion.NEUTRAL,
            "Partial scan",
            f"{missing} was unavailable, so this scan is incomplete. "
            f"{len(result.siblings)} sibling change(s) found, but there may be more.",
        )

    if result.siblings:
        repos = sorted({s.change.repo for s in result.siblings})
        tickets = ", ".join(result.subject.tickets)
        return CheckVerdict(
            Conclusion.NEUTRAL,
            f"{len(result.siblings)} change(s) elsewhere share {tickets}",
            "Advisory, not a gate. Open work in "
            + ", ".join(f"`{r}`" for r in repos)
            + " shares this ticket. See the comment on this pull request.",
        )

    return CheckVerdict(
        Conclusion.SUCCESS,
        "No cross-repo siblings",
        f"Scan complete. No other open change shares {', '.join(result.subject.tickets)}.",
    )
