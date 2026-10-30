"""Did a change make the findings better or worse? Judged briefs, replayed.

Each case is a brief people or sessions judged (`useful`, `noise`, `wrong`,
`missed`), anonymized by `anonymize`, with the model's 0-3 answers stored so a
run asks no model. The run re-ranks each case with the client installed and the
server's promotion threshold, settles the verdict, and counts:

  * useful_flagged  -- useful briefs that say review_with (higher is better)
  * noise_flagged   -- noise briefs that say review_with (lower is better)
  * noise_high      -- of those, at high confidence (lower is better)
  * noise_lead      -- items leading in noise briefs (lower is better)
  * useful_lead     -- items leading in useful briefs (higher is better)

`compare` holds a run to a baseline: worse on any count fails, better passes
and says to record it. Per-case flips are listed either way, so a fix that
trades one case for another is visible, not averaged away.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: Count -> True when a higher value is better.
HIGHER_IS_BETTER = {"useful_flagged": True, "useful_lead": True,
                    "noise_flagged": False, "noise_high": False, "noise_lead": False}


@dataclass
class Run:
    metrics: dict = field(default_factory=dict)
    cases: dict = field(default_factory=dict)      # id -> [rec, conf, lead count]

    def to_dict(self) -> dict:
        return {"metrics": self.metrics, "cases": self.cases}


def read_cases(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def verdict(case: dict, *, promote: int) -> tuple[str, str, list[dict]]:
    from ledger.core import brief as brief_mod
    from ledger.core.rank import items_of, rank
    d = case["brief"]
    scored = {k: v for k, v in (case.get("touches") or {}).items() if v is not None and v >= promote}
    b = brief_mod.ImpactBrief(repo=d["subject"]["repo"], ref=d["subject"]["ref"], tickets=[],
                              recommendation=d["recommendation"], confidence=d["confidence"],
                              reasons=list(d["reasons"]))
    b.linked = tuple(case["linked"]) if case.get("linked") else None
    b.quiet = (case["quiet"][0], case["quiet"][1], list(case["quiet"][2])) if case.get("quiet") else None
    yours = [c["name"] for c in d.get("contracts", [])][:4]
    b.ranked = rank(items_of(d, touches=scored), yours=yours, seen={}).to_dict()
    brief_mod.settle(b)
    return b.recommendation, b.confidence, b.ranked.get("lead") or []


def run(cases: list[dict], *, promote: int = 3) -> Run:
    out = Run()
    m = Counter()
    for c in cases:
        rec, conf, lead = verdict(c, promote=promote)
        out.cases[c["id"]] = [rec, conf, len(lead)]
        flagged = rec == "review_with"
        if c["label"] == "useful":
            m["useful"] += 1
            m["useful_flagged"] += flagged
            m["useful_lead"] += len(lead)
        elif c["label"] == "noise":
            m["noise"] += 1
            m["noise_flagged"] += flagged
            m["noise_high"] += flagged and conf == "high"
            m["noise_lead"] += len(lead)
    out.metrics = {k: m[k] for k in ("useful", "useful_flagged", "useful_lead",
                                      "noise", "noise_flagged", "noise_high", "noise_lead")}
    return out


def compare(now: Run, baseline: dict, labels: dict[str, str]) -> tuple[bool, list[str]]:
    """(no count got worse, report lines)."""
    lines, worse, better = [], [], []
    was = baseline.get("metrics", {})
    for k, up in HIGHER_IS_BETTER.items():
        a, b = was.get(k), now.metrics.get(k)
        if a is None or a == b:
            lines.append(f"  {k:15} {b}")
            continue
        good = (b > a) if up else (b < a)
        (better if good else worse).append(k)
        lines.append(f"  {k:15} {a} -> {b}  {'better' if good else 'WORSE'}")
    flips = []
    for cid, (rec, conf, n) in sorted(now.cases.items()):
        old = (baseline.get("cases") or {}).get(cid)
        if old and (old[0], old[1]) != (rec, conf):
            flips.append(f"  {cid} [{labels.get(cid)}] {old[0]}/{old[1]} -> {rec}/{conf}")
    if flips:
        lines.append(f"{len(flips)} case(s) changed verdict:")
        lines.extend(flips)
    if worse:
        lines.append("worse on: " + ", ".join(worse))
    elif better:
        lines.append("better on: " + ", ".join(better) + " -- record it with --update")
    return not worse, lines
