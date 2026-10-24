"""Replay: what would a brief have said about changes that already merged?

Unit tests prove the code does what it says. This asks the harder question, on
the reader's own history: for a change that really happened, on the day it was
opened, would the brief have said something its author did not already know?

Each merged change is briefed as it was -- its own diff, against the commit it
was merged onto -- with the rest of the estate as it stood on the day the
change started: every other repository at the last commit its default branch
had that day, and the work in flight elsewhere reconstructed from merge
history. Nothing merged later is read, so a finding cannot lean on hindsight.
The verdict logic is the real one; nothing here knows the answer.

What a replay measures is an upper bound: how often there was something to
say. Whether it mattered is a judgement only the people who did the work can
make, so the report asks them, change by change, and keeps three outcomes
apart that look alike from a distance: something to say, nothing found by a
replay that could see enough, and nothing found because it could not.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ledger import service
from ledger.adapters.vcs.base import ForgeUnavailable
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.config import Config
from ledger.core.join import Change, find_siblings
from ledger.core.tickets import extract_tickets
from ledger.pipeline.scan import ScanCache, read_repo
from ledger.pipeline.signals import NOT_A_CHANGE

#: GitHub and Bitbucket word their merge commits differently; both name the branch.
_MERGE = (re.compile(r"Merge pull request #(?P<pr>\d+) from [^/\s]+/(?P<ref>\S+)"),
          re.compile(r"Merged in (?P<ref>\S+) \(pull request #(?P<pr>\d+)\)"),
          re.compile(r"Merge branch '(?P<ref>[^']+)' into "))
#: A squash merge on GitHub: one commit, the pull request's number at the end.
_SQUASH = re.compile(r"\(#(?P<pr>\d+)\)\s*$")

#: What the person who did the work is asked about each finding. The answers
#: are the deliverable of a replay, not a by-product; `ledger.feedback` keeps them.
VERDICTS = {
    "useful": "useful",
    "known": "already known",
    "false_positive": "false positive",
    "insufficient_evidence": "can't verify",
}
#: For a change the replay stayed silent on.
MISSED = "missed"


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
    head_sha: str          # the tip of the branch as merged (the squash commit, for a squash)
    kind: str = "merge"    # merge | squash
    title: str = ""
    #: A squash merge keeps no branch, so the day its work started is unknown:
    #: it is taken as the day it merged, which can only miss in-flight work.
    started_known: bool = True
    #: When its first commit was written, to the second (ISO 8601).
    started_at: str = ""

    @property
    def id(self) -> str:
        """Short and stable, so an answer given later lands on the right change."""
        return hashlib.sha1(f"{self.repo}\0{self.head_sha}".encode()).hexdigest()[:8]


@dataclass
class Case:
    change: Merged
    brief: dict | None = None
    error: str | None = None
    #: The day the rest of the estate was read as of, and what it held then.
    as_of: date | None = None
    checked: list[str] = field(default_factory=list)        # other repositories read that day
    not_yet: list[str] = field(default_factory=list)        # had no commit yet that day
    #: [{kind, text, link}] -- where a reader can check each claim.
    evidence: list[dict] = field(default_factory=list)
    thin: bool = False      # the estate it was read against was too small to tell

    @property
    def strength(self) -> str | None:
        """How much a finding claims, for a reader deciding whether to trust it.

        `conflict`: the change removes a contract or field another repository
        uses, or an open decision governs what it touches. `dependency`: the
        two sides face each other over a named contract, in a known direction.
        `correlation`: only a shared ticket, or the same name appearing in
        both places -- worth a look, and not yet evidence that anything breaks.
        """
        if self.outcome != "context":
            return None
        b = self.brief
        if (b.get("contract_breaks") or b.get("field_breaks")
                or any(i.get("conflict") for i in b.get("impacts", []))
                or any(d.get("gate_hit") for d in b.get("decisions", []))):
            return "conflict"
        directed = any(p.get("uses") in ("publishes", "consumes")
                       for c in b.get("contracts", []) if c.get("also_touched_by")
                       for p in c.get("parties", []))
        if directed or any(i.get("over") for i in b.get("impacts", [])):
            return "dependency"
        return "correlation"

    @property
    def outcome(self) -> str:
        """context | clear | coverage | error.

        `clear` is a claim -- nothing found, by a replay that could see enough
        -- and `coverage` is its absence. A silence is only `clear` when the
        brief could read every repository it was given and there was more
        than one to read.
        """
        if self.error or not self.brief:
            return "error"
        rec = self.brief["recommendation"]
        if rec in ("review_with", "decision_needed"):
            return "context"
        idx = self.brief.get("index") or {}
        if (rec == "insufficient_data" or self.thin or idx.get("unreadable_repos")
                or idx.get("missing_sources")):
            return "coverage"
        return "clear"


@dataclass
class RepoHistory:
    repo: str
    merges: int = 0
    squashes: int = 0
    other: int = 0          # first-parent commits with no pull request in them
    error: str | None = None


@dataclass
class Replay:
    since: date
    author: str | None
    cases: list[Case] = field(default_factory=list)
    merges_seen: int = 0
    repos_seen: int = 0
    source: str = "live"      # live | stored: what each change was briefed against
    #: Coverage: every repository the client could see, and what each held.
    history: list[RepoHistory] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # visible but not characterised
    excluded: list[str] = field(default_factory=list)    # excluded by config
    in_window: int = 0          # merged changes in the window, by anyone
    seconds: float = 0.0

    @property
    def briefed(self) -> list[Case]:
        return [c for c in self.cases if c.brief]

    @property
    def with_context(self) -> list[Case]:
        return [c for c in self.cases if c.outcome == "context"]

    @property
    def clear(self) -> list[Case]:
        return [c for c in self.cases if c.outcome == "clear"]

    @property
    def uncovered(self) -> list[Case]:
        return [c for c in self.cases if c.outcome == "coverage"]

    @property
    def readable(self) -> list[str]:
        return [h.repo for h in self.history if h.error is None]

    def gaps(self) -> list[str]:
        """What this replay could not see, in words a reader can act on."""
        out: list[str] = []
        if len(self.readable) < 2:
            out.append(f"Ledger can see {len(self.readable)} repository on this machine. A replay "
                       "looks for effects in other repositories, so it needs at least two: run "
                       "`aglium-ledger mirrors`, or pass `--local-root` with your clones.")
        bad = [h for h in self.history if h.error]
        if bad:
            out.append("Could not read the history of " + ", ".join(h.repo for h in bad) + ".")
        if self.skipped:
            out.append("Not readable as repositories: " + ", ".join(self.skipped) + ".")
        rebased = [h.repo for h in self.history
                   if not h.error and h.other and not (h.merges or h.squashes)]
        if rebased:
            out.append("Commits with no pull request in them, so nothing to replay "
                       "(rebase-merged or pushed directly): " + ", ".join(rebased) + ".")
        return out


def _git(path: Path, *args: str, timeout: float = 60) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _history(path: Path, repo: str, ref: str, keys, since: date) -> tuple[list[Merged], RepoHistory]:
    """Every pull request merged into a branch since a date, and what else landed there."""
    out: list[Merged] = []
    seen = RepoHistory(repo)
    raw = _git(path, "log", "--first-parent", f"--since={since.isoformat()}",
               "--date=short", "--format=%ad%x09%cd%x09%H%x09%P%x09%aI%x09%s", ref)
    for line in raw.splitlines():
        parts = line.split("\t", 5)
        if len(parts) != 6:
            continue
        authored, committed, sha, parents, authored_at, subject = parts
        parent = parents.split()
        merged_at = datetime.strptime(committed, "%Y-%m-%d").date()
        if len(parent) == 2:
            m = next((p.search(subject) for p in _MERGE if p.search(subject)), None)
            if not m:
                seen.other += 1
                continue
            base_sha, head_sha = parent
            # The first commit the branch added, and who wrote the last one.
            first = _git(path, "log", "--reverse", "--format=%aI",
                         f"{base_sha}..{head_sha}").splitlines()
            who = _git(path, "log", "-1", "--format=%ae", head_sha).strip()
            branch = m.group("ref")
            if NOT_A_CHANGE.match(branch):
                continue            # a release train merging forward is not somebody's change
            seen.merges += 1
            out.append(Merged(
                repo, branch, tuple(extract_tickets(branch, keys)),
                datetime.fromisoformat(first[0]).date() if first else merged_at, merged_at,
                int(m.group("pr")) if m.groupdict().get("pr") else None, who, base_sha, head_sha,
                kind="merge", title=subject, started_at=first[0] if first else ""))
            continue
        if len(parent) != 1:
            continue
        bb = _MERGE[1].search(subject)
        gh = _SQUASH.search(subject)
        if not (bb or gh):
            seen.other += 1
            continue
        branch = bb.group("ref") if bb else f"pr-{gh.group('pr')}"
        if bb and NOT_A_CHANGE.match(branch):
            continue
        pr = int((bb or gh).group("pr"))
        who = _git(path, "log", "-1", "--format=%ae", sha).strip()
        started = datetime.strptime(authored, "%Y-%m-%d").date()
        seen.squashes += 1
        out.append(Merged(
            repo, branch, tuple(extract_tickets(f"{branch} {subject}", keys)),
            min(started, merged_at), merged_at, pr, who, parent[0], sha,
            kind="squash", title=subject, started_known=started < merged_at,
            started_at=authored_at))
    return out, seen


def merged_in(path: Path, repo: str, ref: str, keys, since: date) -> list[Merged]:
    """Every pull request merged into a branch since a date, with the change it closed."""
    return _history(path, repo, ref, keys, since)[0]


def as_of(vcs: LocalGitAdapter, repos: dict[str, str], day: date,
          memo: dict | None = None) -> tuple[dict[str, str], list[str]]:
    """Each repository at the last commit its default branch had by the end of `day`.

    Returns {repo: sha} and the repositories that had no commit yet. Reading
    the estate at these commits is what keeps a replay honest: a contract
    another team started using a month later is not something the author
    could have been told about.
    """
    memo = {} if memo is None else memo
    out: dict[str, str] = {}
    not_yet: list[str] = []
    until = (day + timedelta(days=1)).isoformat()
    for repo, ref in repos.items():
        key = (repo, day)
        if key not in memo:
            sha = ""
            try:
                p = vcs._path(repo)
                sha = _git(p, "rev-list", "-1", "--first-parent", f"--before={until} 00:00",
                           vcs._resolve(p, ref)).strip()
            except ForgeUnavailable:
                sha = ""
            memo[key] = sha
        if memo[key]:
            out[repo] = memo[key]
        else:
            not_yet.append(repo)
    return out, not_yet


class _Memo(ScanCache):
    """Scans of past commits, kept in memory for one replay.

    A past commit never changes, and many changes are read against the same
    commit of a quiet repository, so each is scanned once. Nothing is written
    to the disk cache: that holds today's heads, and a replay must not evict them.
    """

    def __init__(self):
        self.root = Path("/nonexistent")
        self.hits = 0
        self.misses = 0
        self._seen: dict = {}

    def _record(self, vcs, repo, ref):
        sha = vcs.head_sha(repo, ref)
        key = (repo, sha)
        if key in self._seen:
            self.hits += 1
            return self._seen[key]
        self.misses += 1
        self._seen[key] = read_repo(vcs, repo, ref)
        return self._seen[key]


class _AsItWas(service.LiveIndex):
    """The live index, with the in-flight work of one past day.

    Each change in flight is read as it stood that day: from the commit it
    was merged onto to the last commit it had by the end of the day. A
    commit its author wrote later is not part of what could have been seen.
    """

    def __init__(self, vcs, repos, cache, result, population=(), ranges=None, cfg=None,
                 diffs=None):
        super().__init__(vcs, repos, cache)
        self._result = result
        self.use_changes(list(population), cfg)
        self._ranges = ranges or {}
        if diffs is not None:
            self._diffs = diffs         # a past commit's diff never changes: share it

    def inflight(self, repo, ref, tickets):
        return self._result

    def diff_range(self, repo, ref, kind, sha):
        return self._ranges.get((repo, ref))


def _live(vcs):
    cache, diffs = _Memo(), {}
    return lambda repos, result, **kw: _AsItWas(vcs, repos, cache, result, diffs=diffs, **kw)


def _head_on(vcs, m: Merged, day: date, memo: dict) -> str | None:
    """The last commit `m`'s branch had by the end of `day`, or None before its first."""
    key = (m.repo, m.head_sha, day)
    if key not in memo:
        until = (day + timedelta(days=1)).isoformat()
        try:
            p = vcs._path(m.repo)
            memo[key] = _git(p, "rev-list", "-1", f"--before={until} 00:00",
                             f"{m.base_sha}..{m.head_sha}").strip() or None
        except ForgeUnavailable:
            memo[key] = None
    return memo[key]


def me(path: Path) -> str | None:
    return _git(path, "config", "user.email").strip() or None


# --------------------------------------------------------------- evidence --

_REMOTE = re.compile(r"(?:git@|https?://(?:[^@/]+@)?)(?P<host>github\.com|bitbucket\.org)[:/]"
                     r"(?P<path>[^\s]+?)(?:\.git)?/?$")


def web_base(path: Path) -> str | None:
    """The repository's web address on its forge, from its origin remote."""
    m = _REMOTE.search(_git(path, "remote", "get-url", "origin").strip()
                       or _git(path, "config", "remote.origin.url").strip())
    return f"https://{m.group('host')}/{m.group('path')}" if m else None


class Links:
    """Links a reader can open to check a claim, or `repo@sha:path` where there is no forge."""

    def __init__(self, vcs: LocalGitAdapter):
        self.vcs = vcs
        self._bases: dict[str, str | None] = {}

    def _base(self, repo: str) -> str | None:
        if repo not in self._bases:
            try:
                self._bases[repo] = web_base(self.vcs._path(repo))
            except ForgeUnavailable:
                self._bases[repo] = None
        return self._bases[repo]

    def commit(self, repo: str, sha: str) -> str:
        b = self._base(repo)
        if b and "bitbucket.org" in b:
            return f"{b}/commits/{sha}"
        return f"{b}/commit/{sha}" if b else f"{repo}@{sha[:10]}"

    def file(self, repo: str, sha: str, path: str) -> str:
        b = self._base(repo)
        if b and "bitbucket.org" in b:
            return f"{b}/src/{sha}/{path}"
        return f"{b}/blob/{sha}/{path}" if b else f"{repo}@{sha[:10]}:{path}"


def _evidence(m: Merged, b: dict, then: dict[str, str], heads: dict, vcs, links: Links) -> list[dict]:
    """Where each claim in a brief can be checked, at the commits it was read from."""
    out = [{"kind": "change", "text": f"this change: {m.repo} `{m.ref}`"
            + (f" (PR #{m.pr})" if m.pr else ""), "link": links.commit(m.repo, m.head_sha)}]
    for i in b.get("impacts", [])[:5]:
        sha = i.get("sha") or heads.get((i["repo"], i["ref"]))
        out.append({"kind": "in_flight",
                    "text": f"in flight that day: {i['repo']} `{i['ref']}`"
                            + (f" by {i['author']}" if i.get("author") else "")
                            + (f", sharing {', '.join(i['shared_tickets'])}"
                               if i.get("shared_tickets") else "")
                            + (f" ({i['how']})" if i.get("how") else ""),
                    "link": links.commit(i["repo"], sha) if sha else None})
    for c in [c for c in b.get("contracts", []) if c.get("also_touched_by")][:3]:
        for other in c["also_touched_by"][:3]:
            sha = then.get(other)
            paths: list[str] = []
            if sha:
                try:
                    paths = vcs.grep_paths(other, sha, [c["name"]])[:2]
                except ForgeUnavailable:
                    paths = []
            out.append({"kind": "contract",
                        "text": f"{c['surface']} `{c['name']}` is also named in {other}"
                                + (f" ({paths[0]})" if paths else ""),
                        "link": (links.file(other, sha, paths[0]) if paths
                                 else links.commit(other, sha) if sha else None)})
    for d in b.get("decisions", [])[:3]:
        sha = then.get(d["repo"])
        out.append({"kind": "decision",
                    "text": f"decision: {d['title']} ({d['status']}"
                            + (f", deciders {', '.join(d['deciders'])}" if d.get("deciders") else "")
                            + ")",
                    "link": links.file(d["repo"], sha, d["path"]) if sha else f"{d['repo']}:{d['path']}"})
    return out


# -------------------------------------------------------------------- run --

def run(cfg: Config, vcs: LocalGitAdapter, *, since: date, author: str | None,
        limit: int = 30, progress=None, open_index=None, source: str = "live") -> Replay:
    """Brief every change `author` merged since `since`, as of the day it started.

    `author` None replays everyone's. Needs local git -- mirrors or checkouts:
    merge history and old diffs are not something a forge API hands out.

    `open_index(repos, result)` supplies the index a change is briefed against,
    given the in-flight work of its day. The default reads the estate live, as
    it was that day; an organisation server passes its stored index, which is
    today's, and the report says so.
    """
    t0 = time.monotonic()
    as_it_was = open_index is None
    open_index = open_index or _live(vcs)
    repos = service.repos_for(cfg, vcs)
    # Read further back than asked: a change that started before the window
    # was still in flight inside it.
    horizon = since - timedelta(days=cfg.liveness_days)
    every: list[Merged] = []
    history: list[RepoHistory] = []
    for repo, default in repos.items():
        try:
            p = vcs._path(repo)
            got, seen = _history(p, repo, vcs._resolve(p, default), cfg.project_keys, horizon)
        except Exception as e:                          # noqa: BLE001 -- one repo, not the replay
            history.append(RepoHistory(repo, error=f"{type(e).__name__}: {e}"[:160]))
            continue
        every += got
        history.append(seen)
    in_window = [m for m in every if m.merged >= since]
    out = Replay(since=since, author=author, merges_seen=len(every),
                 repos_seen=len({m.repo for m in every}), source=source, history=history,
                 skipped=list(getattr(vcs, "skipped", [])),
                 excluded=sorted(set(cfg.exclude_repos)), in_window=len(in_window))
    mine = sorted((m for m in in_window
                   if author is None or m.author.lower() == author.lower()),
                  key=lambda m: m.merged, reverse=True)[:limit]
    heads = {(m.repo, m.ref): m.head_sha for m in every}
    links = Links(vcs)
    memo: dict = {}
    thin = len(out.readable) < 2

    heads_on: dict = {}

    def as_change(m: Merged, day: date) -> Change:
        head = _head_on(vcs, m, day, heads_on) if as_it_was else None
        return Change(repo=m.repo, ref=m.ref, tickets=m.tickets, kind="pr", pr_number=m.pr,
                      author=m.author, sha=head,
                      updated_at=datetime.fromisoformat(m.started_at) if m.started_at else None)

    def open_on(day: date) -> list[tuple[Merged, Change]]:
        return [(o, as_change(o, day)) for o in every if o.started <= day <= o.merged]

    for i, m in enumerate(mine):
        if progress:
            progress(i + 1, len(mine), m)
        pairs = open_on(m.started)
        population = [c for _, c in pairs]
        subject = next((c for c in population if c.repo == m.repo and c.ref == m.ref),
                       Change(repo=m.repo, ref=m.ref, tickets=m.tickets, kind="pr",
                              pr_number=m.pr, author=m.author))
        result = find_siblings(subject, population)
        # Each other change as it stood that day; one with no commit yet is not readable.
        ranges = {(o.repo, o.ref): (o.base_sha, c.sha) for o, c in pairs if c.sha}
        others = [c for c in population if c is not subject and c.sha]
        case = Case(m, as_of=m.started, thin=thin)
        if as_it_was:
            then, case.not_yet = as_of(vcs, {r: d for r, d in repos.items() if r != m.repo},
                                       m.started, memo)
            scope = {**then, m.repo: m.base_sha}
        else:
            then, scope = {}, repos
        case.checked = sorted(r for r in scope if r != m.repo)
        try:
            own = vcs.commits_ahead(m.repo, m.base_sha, m.head_sha)
            if as_it_was:
                opener = (lambda r, result=result, others=others, ranges=ranges:
                          open_index(r, result, population=others, ranges=ranges, cfg=cfg))
            else:
                opener = lambda r, result=result: open_index(r, result)
            b = service.compute_brief(
                cfg, [vcs], m.repo, m.head_sha, opener,
                subject=service.Subject(scope, m.base_sha, own))
            d = b.to_dict()
            d["subject"]["ref"] = m.ref
            case.brief = d
            if case.outcome == "context":
                case.evidence = _evidence(m, d, then or {}, heads, vcs, links)
        except Exception as e:                          # noqa: BLE001 -- recorded, never fatal
            case.error = f"{type(e).__name__}: {e}"[:200]
        out.cases.append(case)
    out.seconds = time.monotonic() - t0
    return out


# ------------------------------------------------------------------ report --

def _other_repos(b: dict, repo: str) -> list[str]:
    seen = {i["repo"] for i in b.get("impacts", [])}
    for c in b.get("contracts", []):
        seen.update(c.get("also_touched_by") or [])
    return sorted(seen - {repo})


def _ask(case_id: str) -> str:
    boxes = "  ".join(f"[ ] {label}" for label in VERDICTS.values())
    return f"Your verdict (`{case_id}`):  {boxes}"


def _why_empty(r: Replay) -> list[str]:
    """No case at all: say which of the reasons it was, not just that it was empty."""
    if not r.history:
        return ["Ledger could not see any repository on this machine. Run `aglium-ledger init` and "
                "`aglium-ledger mirrors`, or pass `--local-root` with your clones."]
    if r.in_window and r.author:
        return [f"{r.in_window} pull request(s) merged in the window, none of them written by "
                f"{r.author}. Run it with `--everyone` to replay your team's."]
    lines = [f"No merged pull requests were found in {len(r.history)} repositories since "
             f"{r.since.isoformat()}. A replay reads merge commits and squash merges that name "
             "their pull request; an estate that squash-merges everything without that, or "
             "rebases, leaves nothing to read."]
    if any(h.other for h in r.history):
        lines.append("Commits did land in that time, without a pull request in them.")
    lines.append("Try a longer window with `--days`.")
    return [" ".join(lines)]


def render(r: Replay) -> str:
    """The report a stranger can read, with a place to say whether it mattered."""
    briefed, ctx, clear, uncovered = r.briefed, r.with_context, r.clear, r.uncovered
    who = f"merged by {r.author}" if r.author else "merged by anyone"
    by_verdict: dict[str, int] = {}
    for c in briefed:
        by_verdict[c.brief["recommendation"]] = by_verdict.get(c.brief["recommendation"], 0) + 1
    hindsight = r.source == "live"
    lines = [
        "# Ledger replay",
        "",
        f"Changes {who} since {r.since.isoformat()}, each briefed as it was on the day it started: "
        "its own diff, against the commit it was merged onto, "
        + ("with every other repository as it stood that day and the work that was in flight."
           if hindsight else
           "against the organisation index as it is today, with the work that was in flight that day."),
        "",
        "## The result",
        "",
        "```",
        f"{len(briefed):>4} changes replayed",
        f"{len(ctx):>4} with something to say about another repository    (Ledger)",
        f"       {sum(c.strength == 'conflict' for c in ctx):>3} conflict: removes something another repository uses, or an open decision governs it",
        f"       {sum(c.strength == 'dependency' for c in ctx):>3} dependency: faces another repository over a named contract",
        f"       {sum(c.strength == 'correlation' for c in ctx):>3} correlation only: a shared ticket or name, no conflict shown",
        f"{len(clear):>4} where nothing was found, with enough coverage to say so",
        f"{len(uncovered):>4} where Ledger could not see enough to tell         (not the same as safe)",
        "   _ useful: you did not know it and it mattered        (you)",
        "   _ already known                                      (you)",
        "   _ false positive                                     (you)",
        "   _ could not verify from the evidence given           (you)",
        "```",
        "",
        "The first four lines are measured. The rest are yours: tick one box per finding below, "
        "then run `aglium-ledger feedback import " + "ledger-replay.md` to keep the answers on this "
        "machine. Nothing is sent anywhere.",
        "",
        "| Verdict | Changes |",
        "|---|---|",
    ]
    for v in ("decision_needed", "review_with", "insufficient_data", "proceed"):
        if by_verdict.get(v):
            lines.append(f"| `{v}` | {by_verdict[v]} |")
    lines += ["", "## What Ledger could see", ""]
    readable = r.readable
    lines.append(f"- {len(readable)} repositories read"
                 + (": " + ", ".join(readable[:40]) + (" …" if len(readable) > 40 else "")
                    if readable else "") + ".")
    lines.append(f"- {r.in_window} pull requests merged in the window across them, "
                 f"{r.merges_seen} counting the earlier ones still in flight inside it.")
    if r.excluded:
        lines.append("- Excluded by your config: " + ", ".join(r.excluded) + ".")
    for g in r.gaps():
        lines.append(f"- **Gap:** {g}")
    lines.append("- Repositories that are not on this machine are invisible to a replay. "
                 "`aglium-ledger coverage` compares them with your organisation.")
    lines += [
        "",
        "## What this does and does not show",
        "",
        ("- No hindsight: every other repository was read at the last commit its default branch "
         "had on the day each change started. A contract or decision added after that day is "
         "not used." if hindsight else
         "- Which repositories share a contract, and which decisions exist, are read from the "
         "organisation index as it is today, not as it was that day."),
        "- In-flight work is reconstructed from merge history, each change read as it stood "
        "that day. It is linked by a ticket key in a branch name, pull request title or diff; by "
        "a topic or route both diffs name; by a rare topic, table or migration both touch; and, "
        "as a question that never moves a verdict, by the same author starting work around the "
        "same time. Rebased changes leave no trace and are not replayed or counted as in flight. "
        "A squash merge keeps no branch, so its start day is taken as the day it merged.",
        ("- This is a live read, without an organisation index: directions and the fields "
         "another repository reads come from its code, literally; no declared topic wiring, no "
         "documentation links." if hindsight else
         "- Topic wiring, field-level contracts and documentation links are today's too."),
        "- It is an upper bound on usefulness. It says how often there was something to say.",
        f"- Took {r.seconds:.0f} s." if r.seconds else "",
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
            (f"> {m.title}\n" if m.kind == "squash" and m.title else "")
            + f"Merged {m.merged.isoformat()}, started {m.started.isoformat()}"
            + ("" if m.started_known else " (unknown: squash merge)")
            + (f", PR #{m.pr}" if m.pr else "") + f" · **{b['recommendation']}** "
            f"(confidence {b['confidence']})"
            + (f" · other repositories: {', '.join(others[:6])}" if others else ""),
            "",
        ]
        lines.append(f"Strength: **{c.strength}**. " + {
            "conflict": "The evidence shows something another repository uses being removed, "
                        "or a decision still open over what it touches.",
            "dependency": "The two sides face each other over a named contract; whether they "
                          "agree is for the people involved.",
            "correlation": "Only a shared ticket or a name in both places. Worth a look; not yet "
                           "evidence that anything conflicts.",
        }[c.strength])
        lines.append("")
        lines += [f"- {reason}" for reason in b.get("reasons", [])[:5]]
        if c.evidence:
            lines += ["", f"Evidence, as of {c.as_of.isoformat()}:" if hindsight and c.as_of
                      else "Evidence:", ""]
            for e in c.evidence:
                lines.append(f"- {e['text']}" + (f": {e['link']}" if e.get("link") else ""))
        lines += ["", _ask(m.id), ""]
    if uncovered:
        lines += ["## Changes Ledger could not see enough to judge", "",
                  "Not a clean result: Ledger was missing repositories or history it needed. "
                  "See \"What Ledger could see\" for what to add.", ""]
        for c in uncovered:
            idx = c.brief.get("index") or {}
            why = ("only one repository was visible" if c.thin else
                   "unreadable: " + ", ".join(idx.get("unreadable_repos") or []) if idx.get("unreadable_repos")
                   else "sources missing: " + ", ".join(idx.get("missing_sources") or [])
                   if idx.get("missing_sources") else "; ".join(c.brief.get("reasons", [])[:1])
                   or "insufficient data")
            lines.append(f"- {c.change.merged.isoformat()} {c.change.repo} `{c.change.ref}`: {why}")
        lines.append("")
    if clear:
        lines += ["## Changes where nothing was found", "",
                  "Each was checked against the other repositories as they were that day. If one "
                  "of these broke something in another repository, that is a **miss**, and the "
                  "most useful thing a replay can find. Mark it.", ""]
        lines += [f"- [ ] {MISSED} (`{c.change.id}`) · {c.change.merged.isoformat()} "
                  f"{c.change.repo} `{c.change.ref}` · checked against {len(c.checked)} "
                  "other repositories" for c in clear]
        lines.append("")
    failed = [c for c in r.cases if c.error]
    if failed:
        lines += ["## Could not be replayed", ""]
        lines += [f"- {c.change.repo} `{c.change.ref}`: {c.error}" for c in failed]
        lines.append("")
    if not r.cases:
        lines += ["## Nothing to replay", "", *_why_empty(r), ""]
    if r.source == "live" and r.cases:
        from ledger import upgrade
        lines += upgrade.report_section()
    return "\n".join(lines)


def link_kind(how: str | None) -> str:
    """Which rule linked an in-flight item: ticket, ticket_in_diff, contract, signal or author."""
    if not how:
        return "ticket"
    for prefix, kind in (("its diff also names", "contract"), ("both change", "signal"),
                         ("same author", "author")):
        if how.startswith(prefix):
            return kind
    return "ticket_in_diff" if "which this diff names" in how else "other"


def metrics(r: Replay) -> dict:
    """The measured half of a replay, as numbers: what was found, and how."""
    ctx = r.with_context
    links: dict[str, int] = {}
    for c in r.briefed:
        for i in c.brief.get("impacts", []):
            k = link_kind(i.get("how"))
            links[k] = links.get(k, 0) + 1
    return {
        "replayed": len(r.briefed), "with_context": len(ctx), "clear": len(r.clear),
        "coverage": len(r.uncovered), "errors": sum(1 for c in r.cases if c.error),
        "by_strength": {s: sum(c.strength == s for c in ctx)
                        for s in ("conflict", "dependency", "correlation")},
        "by_recommendation": {v: sum(c.brief["recommendation"] == v for c in r.briefed)
                              for v in ("decision_needed", "review_with", "insufficient_data",
                                        "proceed")},
        "linked_in_flight": links,
        "field_breaks": sum(len(c.brief.get("field_breaks", [])) for c in r.briefed),
        "contract_breaks": sum(len(c.brief.get("contract_breaks", [])) for c in r.briefed),
        "cannot_both_land": sum(1 for c in r.briefed for i in c.brief.get("impacts", [])
                                if i.get("conflict")),
        "seconds": round(r.seconds, 1),
    }


def cases_json(r: Replay) -> dict:
    """The cases, without any code: what `feedback` needs to file an answer by its id."""
    return {
        "since": r.since.isoformat(), "author": r.author, "source": r.source,
        "seconds": round(r.seconds, 1),
        "repos_read": len(r.readable), "gaps": r.gaps(), "metrics": metrics(r),
        "cases": [{
            "id": c.change.id, "repo": c.change.repo, "ref": c.change.ref,
            "sha": c.change.head_sha, "merged": c.change.merged.isoformat(),
            "recommendation": c.brief["recommendation"] if c.brief else None,
            "outcome": c.outcome, "strength": c.strength,
            "links": sorted({link_kind(i.get("how")) for i in (c.brief or {}).get("impacts", [])}),
        } for c in r.cases],
    }


def write(r: Replay, out: str | Path) -> tuple[Path, Path]:
    """The report, and the cases beside it for `feedback`."""
    md = Path(out)
    md.write_text(render(r))
    side = md.with_suffix(".json")
    side.write_text(json.dumps(cases_json(r), indent=2))
    return md, side
