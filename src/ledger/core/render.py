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


#: Lines per section. The comment is read mid-review; past this it is skimmed.
_SHOWN = 5

_HEADLINE = {
    "decision_needed": "needs a decision before it merges",
    "review_with": "affects work in other repositories",
}


def render_brief_comment(brief: dict, *, marker: str = "ledger:siblings") -> str | None:
    """The PR comment from a full brief, or None when there is nothing to say.

    The sibling comment above can only say "same ticket". This one says what
    the brief found whatever linked it: a field or contract this change
    removes that another repository reads, another change in flight that
    cannot land with this one, the repositories on the other side of a topic
    or route, and work linked by ticket, by what it touches, or by author.
    Names, directions and paths only; never another repository's code.
    """
    rec = brief.get("recommendation")
    if rec not in _HEADLINE:
        return None                     # silence beats "nothing found"
    lines = [f"**Ledger: this change {_HEADLINE[rec]}** · `{rec}` "
             f"(confidence {brief.get('confidence', 'low')})", ""]
    lines += [f"- {r}" for r in (brief.get("reasons") or [])[:_SHOWN]]

    breaks = [f"removes `{b['field_path']}` from {b['surface']} `{b['name']}`; read by "
              + ", ".join(f"`{r['repo']}`" + (f" ({r['evidence']})" if r.get("evidence") else "")
                          for r in b.get("readers", [])[:3])
              for b in brief.get("field_breaks", [])]
    breaks += [f"stops naming {c['surface']} `{c['name']}` ({c['path']}); also used by "
               + ", ".join(f"`{r}`" for r in c.get("also_touched_by", [])[:3])
               for c in brief.get("contract_breaks", [])]
    if breaks:
        lines += ["", "**What another repository loses**", ""] + [f"- {b}" for b in breaks[:_SHOWN]]

    impacts = brief.get("impacts", [])
    if impacts:
        lines += ["", "**Other work in flight**", "",
                  "| Repo | Change | Author | Why linked | How they meet |", "|---|---|---|---|---|"]
        for i in impacts[:_SHOWN]:
            why = i.get("how") or ("shares " + ", ".join(i.get("shared_tickets") or []) or "—")
            meet = i.get("conflict") and f"**cannot both land:** {i['conflict']}" \
                or i.get("relation") or "—"
            lines.append(f"| `{i['repo']}` | `{i['ref']}` | {i.get('author') or '—'} | "
                         f"{_cell(why)} | {_cell(meet)} |")

    faced = []
    for c in brief.get("contracts", []):
        if not c.get("also_touched_by"):
            continue
        parties = [p for p in c.get("parties", []) if p.get("uses") in ("publishes", "consumes")]
        who = ", ".join(f"`{p['repo']}` {p['uses']}" for p in parties[:3]) \
            or ", ".join(f"`{r}`" for r in c["also_touched_by"][:3]) + " (direction unknown)"
        faced.append(f"{c['surface']} `{c['name']}`: {who}")
    if faced:
        lines += ["", "**Contracts this change touches, and who else uses them**", ""]
        lines += [f"- {f}" for f in faced[:_SHOWN]]

    lines += ["", "_Read from the code by the Ledger client: names, directions and paths, no code. "
                  "Advisory; it never fails a build._"]
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:220]
