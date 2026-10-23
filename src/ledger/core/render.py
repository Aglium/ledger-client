"""The PR comment.

This is the highest-stakes design surface in the system. It is read by someone
mid-review who did not ask for it, and it gets exactly one chance.

Two rules drive the shape:
  1. It is an introduction between two people, not a verdict. A comment that
     reads as a judgement gets dismissed reflexively; one that names the other
     author and their ticket gets acted on.
  2. Partial results say so. A short list presented as complete is a false
     negative in a tool whose entire job is telling you what you cannot see.
"""

from __future__ import annotations

from ledger.core.join import JoinResult


def render_siblings(result: JoinResult, *, marker: str = "ledger:siblings") -> str | None:
    """Return the comment body, or None when there is nothing worth saying.

    Returning None matters: posting "no siblings found" on every PR is how a bot
    gets muted in week two.
    """
    if not result.siblings:
        if result.partial:
            # Say nothing rather than imply "all clear" from a degraded scan.
            return None
        return None

    subject = result.subject
    tickets = ", ".join(subject.tickets)
    n = len(result.siblings)
    #: A long table is skimmed, not read. Show the most recent and say how many
    #: more there are; the point is "go look", not an exhaustive inventory.
    MAX_ROWS = 8
    lines = [
        f"**Also in flight for {tickets}** — {n} other change{'s' if n != 1 else ''} "
        f"in {len(({s.change.repo for s in result.siblings}))} other repo"
        f"{'s' if len({s.change.repo for s in result.siblings}) != 1 else ''}.",
        "",
        "| Repo | Change | Author | Shares |",
        "|---|---|---|---|",
    ]
    shown = result.siblings[:MAX_ROWS]
    for s in shown:
        c = s.change
        if c.kind == "integration":
            # Merged to an integration branch but not released. Saying "branch"
            # here would misrepresent its state -- this work is already done.
            where = f"in `{c.integration_ref}`" if c.integration_ref else "integrated"
            label = f"{where} · `{c.ref}`"
            # "Merge pull request #100 from org/feat/x" adds nothing when the
            # ref is already `feat/x`. Only a real subject earns the space.
            title = c.title or ""
            detail = "" if (not title or c.ref in title or title.startswith("Merge pull request")) \
                else f" — {title}"
            ref = f"{label}{detail}"
        else:
            label = f"#{c.pr_number}" if c.pr_number else "branch"
            ref = f"[{label} `{c.ref}`]({c.url})" if c.url else f"{label} `{c.ref}`"
        lines.append(f"| `{c.repo}` | {ref} | {c.author or '—'} | {', '.join(s.shared_tickets)} |")

    if len(result.siblings) > MAX_ROWS:
        lines.append(f"| … | _{len(result.siblings) - MAX_ROWS} more_ | | |")

    integrated = sum(1 for s in result.siblings if s.change.kind == "integration")
    note = "_Same ticket, different repo. Worth a look before this merges._"
    if integrated:
        note = (f"_Same ticket, different repo. {integrated} of these are already merged to an "
                "integration branch but not released._")
    lines += ["", note]

    if result.partial:
        missing = ", ".join(result.missing_sources) or "one or more sources"
        lines += ["", f"> ⚠️ **Partial scan** — {missing} was unavailable. There may be more than this."]

    return "\n".join(lines)
