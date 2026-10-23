"""Replay: what would a brief have said about changes that already merged?

Unit tests prove the code does what it says. This asks the harder question, on
the reader's own history: for a change that really happened, on the day it was
opened, would the brief have said something its author did not already know?

Each merged change is briefed as it was -- its own diff, against the commit it
was merged onto -- with the work that was in flight elsewhere on the day it
started, reconstructed from merge history. The verdict logic is the real one;
nothing here knows the answer.

Two things are read from today rather than from that day, and the report says
so: which other repositories touch a contract, and which decisions exist. A
contract somebody started sharing since will show as shared.

What a replay measures is an upper bound: how often there was something to
say. Whether it would have changed anything is a judgement only the people
who did the work can make, so the report asks them, change by change.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ledger import service
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.config import Config
from ledger.core.join import Change, find_siblings
from ledger.core.tickets import extract_tickets
from ledger.pipeline.scan import ScanCache
from ledger.pipeline.signals import NOT_A_CHANGE

#: GitHub and Bitbucket word their merge commits differently; both name the branch.
_MERGE = (re.compile(r"Merge pull request #(?P<pr>\d+) from [^/\s]+/(?P<ref>\S+)"),
          re.compile(r"Merged in (?P<ref>\S+) \(pull request #(?P<pr>\d+)\)"),
          re.compile(r"Merge branch '(?P<ref>[^']+)' into "))

#: What their engineers are asked, per change. The classification is the
#: deliverable of a replay, not a by-product.
CLASSES = ("prevented a defect", "avoided rework", "sped up review", "useful, no change", "noise")


@dataclass(frozen=True)
class Merged:
    repo: str
    ref: str
    tickets: tuple
    started: date
    merged: date
    pr: int | None
    author: str            # email of whoever wrote the branch, not whoever pressed merge
    base_sha: str          # the commit it was merged onto
    head_sha: str          # the tip of the branch as merged


@dataclass
class Case:
    change: Merged
    brief: dict | None = None
    error: str | None = None


@dataclass
class Replay:
    since: date
    author: str | None
    cases: list[Case] = field(default_factory=list)
    merges_seen: int = 0
    repos_seen: int = 0
    source: str = "live"      # live | stored: what each change was briefed against

    @property
    def briefed(self) -> list[Case]:
        return [c for c in self.cases if c.brief]

    @property
    def with_context(self) -> list[Case]:
        return [c for c in self.briefed if c.brief["recommendation"] != "proceed"]


def _git(path: Path, *args: str, timeout: float = 60) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def merged_in(path: Path, repo: str, ref: str, keys, since: date) -> list[Merged]:
    """Every merge commit on a branch since a date, with the change it closed."""
    out: list[Merged] = []
    raw = _git(path, "log", "--merges", "--first-parent", f"--since={since.isoformat()}",
               "--date=short", "--format=%ad%x09%H%x09%P%x09%s", ref)
    for line in raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        merged_str, _sha, parents, subject = parts
        parent = parents.split()
        m = next((p.search(subject) for p in _MERGE if p.search(subject)), None)
        if not m or len(parent) != 2:
            continue
        base_sha, head_sha = parent
        merged_at = datetime.strptime(merged_str, "%Y-%m-%d").date()
        # The first commit the branch added, and who wrote the last one.
        first = _git(path, "log", "--reverse", "--date=short", "--format=%ad",
                     f"{base_sha}..{head_sha}").splitlines()
        who = _git(path, "log", "-1", "--format=%ae", head_sha).strip()
        branch = m.group("ref")
        if NOT_A_CHANGE.match(branch):
            continue            # a release train merging forward is not somebody's change
        out.append(Merged(
            repo, branch, tuple(extract_tickets(branch, keys)),
            datetime.strptime(first[0], "%Y-%m-%d").date() if first else merged_at, merged_at,
            int(m.group("pr")) if m.groupdict().get("pr") else None, who, base_sha, head_sha))
    return out


class _AsItWas(service.LiveIndex):
    """The live index, with the in-flight work of one past day."""

    def __init__(self, vcs, repos, cache, result):
        super().__init__(vcs, repos, cache)
        self._result = result

    def inflight(self, repo, ref, tickets):
        return self._result


def _live(vcs):
    cache = ScanCache.default()
    return lambda repos, result: _AsItWas(vcs, repos, cache, result)


def me(path: Path) -> str | None:
    return _git(path, "config", "user.email").strip() or None


def run(cfg: Config, vcs: LocalGitAdapter, *, since: date, author: str | None,
        limit: int = 30, progress=None, open_index=None, source: str = "live") -> Replay:
    """Brief every change `author` merged since `since`, as of the day it started.

    `author` None replays everyone's. Needs local git -- mirrors or checkouts:
    merge history and old diffs are not something a forge API hands out.

    `open_index(repos, result)` supplies the index a change is briefed against,
    given the in-flight work of its day. The default reads the estate live; an
    organisation server passes its stored index, which also knows the wiring,
    the field-level contracts and the documentation.
    """
    open_index = open_index or _live(vcs)
    repos = service.repos_for(cfg, vcs)
    # Read further back than asked: a change that started before the window
    # was still in flight inside it.
    horizon = since - timedelta(days=cfg.liveness_days)
    every: list[Merged] = []
    for repo, default in repos.items():
        try:
            every += merged_in(vcs._path(repo), repo, default, cfg.project_keys, horizon)
        except Exception:                               # noqa: BLE001 -- one repo, not the replay
            continue
    out = Replay(since=since, author=author, merges_seen=len(every),
                 repos_seen=len({m.repo for m in every}), source=source)
    mine = sorted((m for m in every if m.merged >= since
                   and (author is None or m.author.lower() == author.lower())),
                  key=lambda m: m.merged, reverse=True)[:limit]

    def open_on(day: date) -> list[Change]:
        return [Change(repo=m.repo, ref=m.ref, tickets=m.tickets, kind="pr", pr_number=m.pr,
                       author=m.author) for m in every if m.started <= day <= m.merged]

    for i, m in enumerate(mine):
        if progress:
            progress(i + 1, len(mine), m)
        population = open_on(m.started)
        subject = next((c for c in population if c.repo == m.repo and c.ref == m.ref),
                       Change(repo=m.repo, ref=m.ref, tickets=m.tickets, kind="pr",
                              pr_number=m.pr, author=m.author))
        result = find_siblings(subject, population)
        try:
            own = vcs.commits_ahead(m.repo, m.base_sha, m.head_sha)
            b = service.compute_brief(
                cfg, [vcs], m.repo, m.head_sha,
                lambda r, result=result: open_index(r, result),
                subject=service.Subject(repos, m.base_sha, own))
            d = b.to_dict()
            d["subject"]["ref"] = m.ref
            out.cases.append(Case(m, brief=d))
        except Exception as e:                          # noqa: BLE001 -- recorded, never fatal
            out.cases.append(Case(m, error=f"{type(e).__name__}: {e}"[:200]))
    return out


# ------------------------------------------------------------------ report --

def _other_repos(b: dict, repo: str) -> list[str]:
    seen = {i["repo"] for i in b.get("impacts", [])}
    for c in b.get("contracts", []):
        seen.update(c.get("also_touched_by") or [])
    return sorted(seen - {repo})


def render(r: Replay) -> str:
    """The report a stranger can read, with a place to say whether it mattered."""
    briefed, ctx = r.briefed, r.with_context
    who = f"merged by {r.author}" if r.author else "merged by anyone"
    by_verdict: dict[str, int] = {}
    for c in briefed:
        by_verdict[c.brief["recommendation"]] = by_verdict.get(c.brief["recommendation"], 0) + 1
    lines = [
        "# Ledger replay",
        "",
        f"Changes {who} since {r.since.isoformat()}, each briefed as it was on the day it started: "
        "its own diff, against the commit it was merged onto, with the work that was in flight "
        "in other repositories that day.",
        "",
        "## The funnel",
        "",
        "```",
        f"{len(briefed):>4} changes replayed",
        f"{len(ctx):>4} with cross-repository context                      (Ledger, automatic)",
        "   _ judged materially relevant                          (you)",
        "   _ that would have changed the implementation or review (you)",
        "   _ that match rework or an incident that happened       (your history)",
        "```",
        "",
        "The first two lines are measured. The last three are yours to fill in, from the cases "
        "below: for each one, **would knowing this have changed what you did?**",
        "",
        "| Verdict | Changes |",
        "|---|---|",
    ]
    for v in ("decision_needed", "review_with", "insufficient_data", "proceed"):
        if by_verdict.get(v):
            lines.append(f"| `{v}` | {by_verdict[v]} |")
    lines += [
        "",
        "## What this does and does not show",
        "",
        f"- Read from {r.repos_seen} repositories with merge history in the window, "
        f"{r.merges_seen} merged changes in all.",
        "- In-flight work is reconstructed from merge commits, so it is linked by the ticket key "
        "in a branch name. Squash-merged and rebased changes leave no merge commit and are not "
        "replayed or counted as in flight.",
        "- Which repositories share a contract, and which decisions exist, are read from today's "
        "default branches, not from that day's.",
        ("- This is a live read, without an organisation index: no declared topic wiring, no "
         "field-level contracts, no documentation links." if r.source == "live" else
         "- Briefed against the organisation index as it is today: topic wiring, field-level "
         "contracts and documentation links are today's too."),
        "- It is an upper bound on usefulness. It says how often there was something to say.",
        "",
    ]
    if ctx:
        lines += ["## Changes with something to say", ""]
    for c in ctx:
        m, b = c.change, c.brief
        others = _other_repos(b, m.repo)
        lines += [
            f"### {m.repo} · `{m.ref}`",
            "",
            f"Merged {m.merged.isoformat()}, started {m.started.isoformat()}"
            + (f", PR #{m.pr}" if m.pr else "") + f" · **{b['recommendation']}** "
            f"(confidence {b['confidence']})"
            + (f" · other repositories: {', '.join(others[:6])}" if others else ""),
            "",
        ]
        lines += [f"- {reason}" for reason in b.get("reasons", [])[:5]]
        for i in b.get("impacts", [])[:5]:
            lines.append(f"- in flight that day: {i['repo']} `{i['ref']}`"
                         + (f" ({i['author']})" if i.get("author") else ""))
        lines += ["", "Would knowing this have changed what you did?  "
                  + "  ".join(f"[ ] {k}" for k in CLASSES), ""]
    quiet = [c for c in briefed if c not in ctx]
    if quiet:
        lines += ["## Changes it would have stayed silent on", "",
                  "If one of these broke something in another repository, that is a **miss**, and "
                  "the most useful thing a replay can find. Mark it.", ""]
        lines += [f"- [ ] missed · {c.change.merged.isoformat()} {c.change.repo} `{c.change.ref}`"
                  for c in quiet]
        lines.append("")
    failed = [c for c in r.cases if c.error]
    if failed:
        lines += ["## Could not be replayed", ""]
        lines += [f"- {c.change.repo} `{c.change.ref}`: {c.error}" for c in failed]
        lines.append("")
    if not r.cases:
        lines += ["No merged changes were found in the window. Merge commits are what a replay "
                  "reads: an estate that squash-merges everything has none.", ""]
    if r.source == "live" and r.cases:
        from ledger import upgrade
        lines += upgrade.report_section()
    return "\n".join(lines)
