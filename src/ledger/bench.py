"""Replay labelled briefs through the ranking rules, before a change ships.

A case file is one JSON object per line: an item a past session was shown, as
it was shown, with what the session then did with it. The fields read here:

    sid      the session it was shown in
    branch   the branch that session was on
    n        the item's position in what that session was shown (0: none)
    text     the item exactly as rendered
    label    useful | cleared | noise | neutral | negative
    h2_touches  optional: a model's 0-3 on whether it touches the diff

The labels come from the people and sessions that did the work, so a case
file belongs to the organisation and is never kept in this repository. The
bench reads one wherever it is.

What it measures is the trade the ranking makes: useful items kept in the
lead against noise kept out of it, and precision of what leads. Optionally it
also replays a sample through a simulated reviewer and a judge that sees the
labels (`judge`), which is the only way to see a regression like the one the
ranking was built around -- a single weak item, shown alone, read as the
finding.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ledger.core.rank import CAP, Item, rank


def read_cases(path: str | Path) -> list[dict]:
    with open(Path(path).expanduser(), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def item_of(case: dict, *, touches: bool = False) -> Item:
    """An item as `core.rank` sees it, read back from the text it was shown as."""
    t = case["text"]
    head = t.split("\n")[0]
    score = case.get("h2_touches") if touches else None
    m = re.match(r"in flight: (\S+) (\S+)( \(in (\S+?), ([0-9a-f]{7,40})\))?(?: by (\S+))?", head)
    if m:
        relation = next((l for l in t.split("\n") if l.startswith("relation:")), "")
        if relation and "found no topic, route or table named by both diffs" not in relation:
            link = "relation"
        elif "same author, pushed around the same time" in head:
            link = "author"
        elif "which this diff names" in head:
            link = "diff_ticket"
        elif m.group(3):
            link = "integration"
        else:
            link = "ticket"
        key = f"impact:{m.group(1)}:{m.group(5) or m.group(2)}"
        return Item(key=key, kind="impact", label=f"{m.group(1)} {m.group(5) or m.group(2)}",
                    link=link, author=m.group(6), touches=score)
    if head.startswith(("decision ", "a decision governing", "a decision filed under")):
        return Item(key=f"decision:{head[:80]}", kind="decision", label=head[:60], touches=score)
    if head.startswith("shared contract"):
        directed = bool(re.search(r"\b(publishes|consumes) \(", t))
        return Item(key=f"contract:{head[:80]}", kind="contract", label=head[:60],
                    directed=directed, touches=score)
    if "is also touched on `" in head and "in this repository" in head:
        return Item(key=f"local:{head[:80]}", kind="local", label=head[:60], touches=score)
    if "is documented in" in head:
        return Item(key=f"doc:{head[:80]}", kind="doc", label=head[:60], touches=score)
    # Counts, notes about the change itself, ticket spread: lines of the
    # verdict, not items a reader is sent to weigh.
    return Item(key=f"meta:{head[:80]}", kind="meta", label=head[:60])


@dataclass
class Score:
    name: str
    shown: int = 0
    useful: int = 0
    useful_total: int = 0
    noise_out: int = 0
    noise_total: int = 0
    sessions_nothing: int = 0
    sessions: int = 0
    lost: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.useful / self.shown if self.shown else 0.0

    def row(self) -> str:
        return (f"| {self.name} | {self.shown} | {self.useful}/{self.useful_total} | "
                f"{self.noise_out}/{self.noise_total} | {self.precision:.0%} | "
                f"{self.sessions_nothing}/{self.sessions} |")


def _sessions(cases: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        if c.get("n"):
            by[c["sid"]].append(c)
    return by


def score(cases: list[dict], *, name: str = "ranked", touches: bool = False,
          repeats: bool = False, cap: int = CAP, everything: bool = False) -> Score:
    """What leads under the current rules, against the labels.

    `everything` scores the old behaviour: every item shown. `repeats` treats
    an item already shown in an earlier session on the same branch as named
    once, in file order -- the order sessions happened in.
    """
    s = Score(name)
    labels = Counter(c["label"] for c in cases if c.get("n"))
    s.useful_total, s.noise_total = labels["useful"], labels["noise"]
    seen_on: dict[str, dict[str, str]] = defaultdict(dict)
    for sid, rows in _sessions(cases).items():
        s.sessions += 1
        items = [item_of(r, touches=touches) for r in rows]
        # Context answered from a plan or a prompt is not ranked: it is shown
        # as it always was, in every variant.
        unranked = {r["n"] for r in rows if r.get("verdict") == "PLAN"}
        if everything:
            shown = {r["n"] for r in rows}
        else:
            seen = seen_on[rows[0]["branch"]] if repeats else {}
            got = rank([i for r, i in zip(rows, items) if i.kind != "meta" and r["n"] not in unranked],
                       seen=seen, cap=cap)
            lead = {x["key"] for x in got.lead}
            shown = {r["n"] for r, i in zip(rows, items) if i.key in lead} | unranked
            if repeats:
                for k in lead:
                    seen.setdefault(k, sid)
            s.sessions_nothing += int(got.nothing)
        for r, i in zip(rows, items):
            if r["n"] in shown:
                s.shown += 1
                s.useful += r["label"] == "useful"
            else:
                s.noise_out += r["label"] == "noise"
                if r["label"] == "useful":
                    s.lost.append(f"{sid[:8]} {i.kind}/{i.link or '-'}")
    return s


def table(cases: list[dict]) -> str:
    """The comparison the bench exists for: before, and the ranking as it ships."""
    rows = [score(cases, name="everything shown (before)", everything=True),
            score(cases, name="ranked, rules only"),
            score(cases, name="ranked, rules + model gate", touches=True),
            score(cases, name="ranked, rules + model gate + repeats", touches=True, repeats=True)]
    out = ["| Variant | Shown | Useful kept | Noise kept out | Precision | Sessions with nothing to lead |",
           "|---|---|---|---|---|---|"]
    out += [r.row() for r in rows]
    for r in rows[1:]:
        if r.lost:
            out.append(f"\n{r.name}, useful items not led with: {', '.join(r.lost)}")
    return "\n".join(out)


# ------------------------------------------------------------------- judge --

def claude(prompt: str, *, system: str, model: str = "opus", effort: str = "medium",
           cwd: str | None = None) -> str:
    """One answer from the Claude Code CLI: no tools, no settings, no session kept."""
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--system-prompt", system,
           "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
           "--output-format", "json", "--effort", effort]
    from ledger.core.redact import redact
    for _ in range(3):
        try:
            r = subprocess.run(cmd, input=redact(prompt).text, text=True, capture_output=True,
                               timeout=900, cwd=cwd)
            d = json.loads(r.stdout)
            if not d.get("is_error"):
                return d.get("result") or ""
        except Exception:                               # noqa: BLE001 -- retried, then empty
            continue
    return ""


REVIEWER = ("You are reviewing a code change. You cannot open other repositories. Below is the change "
            "and a cross-repository brief from a tool. Write the cross-repo part of your review in at most "
            "180 words: what in the brief matters for this change and why, what does not apply, and the "
            "concrete next step (who to ask, what to check, which must land first).")
JUDGE = ("You score a reviewer's cross-repo note. You see the change, the brief the reviewer got, and the "
         "ground truth: which brief items turned out useful in the real review, which were noise, and "
         "anything missed. Score 0-2 each: (a) raises the points the useful items supported, or, if none "
         "were useful, says so plainly; (b) does not let noise items drive its conclusions or ask people to "
         "chase them; (c) makes no claim the brief and change do not support; (d) gives a concrete next "
         "step that would actually help. Return only JSON: {\"a\":0,\"b\":0,\"c\":0,\"d\":0,\"why\":\"...\"}")


def render_before(rows: list[dict], head: str) -> str:
    return "\n".join([head] + [f"- {r['text']}" for r in rows])


def render_ranked(rows: list[dict], head: str, *, touches: bool = True) -> str:
    """The lead, each with how to check it, and the rest in one counted line."""
    items = [item_of(r, touches=touches) for r in rows]
    got = rank([i for i in items if i.kind != "meta"])
    lead = {x["key"]: x for x in got.lead}
    subject = head.split(": ")[0] if head.startswith("Ledger brief for") else "Ledger brief"
    if got.nothing:
        lines = [f"{subject}: nothing in flight or pending that this diff touches"]
    else:
        lines = [head.replace("DECISION_NEEDED", "REVIEW_WITH"),
                 "NOT VISIBLE FROM THIS REPOSITORY — weigh these first:"]
        for r, i in zip(rows, items):
            if i.key in lead:
                lines.append(f"- {r['text']}\n    {lead[i.key]['check']}")
        lines.append("Say what each one means for this change, or say why it does not apply.")
    if got.also.get("count"):
        kinds = sorted(got.also["by_kind"].items(), key=lambda kv: -kv[1])
        lines.append("- also seen, not linked to your diff: "
                     + ", ".join(f"{n} {k}(s)" for k, n in kinds)
                     + " — do not chase these without a reason from the diff")
    return "\n".join(lines)


def judge(cases: list[dict], context: dict[str, str], heads: dict[str, str], sids: list[str], *,
          samples: int = 2, cwd: str | None = None) -> list[dict]:
    """Reviewer and judge on each session in `sids`, before and ranked."""
    by = _sessions(cases)
    out = []
    for sid in sids:
        rows = by.get(sid) or []
        if not rows or sid not in context:
            continue
        truth = "\n".join(
            ["USEFUL in the real review:"]
            + [f"- {r['text'].splitlines()[0][:200]} -> {r.get('why', '')}" for r in rows if r["label"] == "useful"]
            + ["NOISE / ruled out:"]
            + [f"- {r['text'].splitlines()[0][:160]}" for r in rows if r["label"] in ("noise", "cleared")][:25])
        for variant, text in (("before", render_before(rows, heads.get(sid, ""))),
                              ("ranked", render_ranked(rows, heads.get(sid, "")))):
            for k in range(samples):
                note = claude(context[sid] + "\n\nBRIEF:\n" + text, system=REVIEWER, cwd=cwd)
                raw = claude(f"CHANGE:\n{context[sid]}\n\nBRIEF GIVEN:\n{text}\n\nGROUND TRUTH:\n{truth}"
                             f"\n\nREVIEWER NOTE:\n{note}", system=JUDGE, cwd=cwd)
                m = re.search(r"\{.*\}", raw, re.S)
                try:
                    j = json.loads(m.group(0)) if m else {}
                except ValueError:
                    j = {}
                out.append({"sid": sid, "variant": variant, "sample": k,
                            "score": sum(int(j.get(x, 0)) for x in "abcd") if j else None,
                            "why": j.get("why", "")})
    return out
