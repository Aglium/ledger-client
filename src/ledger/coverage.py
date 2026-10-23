"""What Ledger can see from this machine, and what it cannot.

An answer is only as good as the estate behind it. A brief that says
`proceed` after reading one repository has said almost nothing, and a reader
cannot tell that from the answer alone. So before the first replay, and any
time an answer looks too quiet, this says exactly which repositories Ledger
inspected, how much history each holds, and where the gaps are -- and checks
the few things the client needs to run at all.

Nothing here is sent anywhere. Listing the organisation on its forge, to find
repositories that are not on this machine, is the one network call, and it is
only made when asked for.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ledger.adapters.vcs.base import ForgeUnavailable
from ledger.config import Config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    #: A failed check that stops Ledger working, rather than one that limits it.
    required: bool = True


def prerequisites(config_path: str | None = None) -> list[Check]:
    """What the client needs on this machine, each with what to do if it is missing."""
    out: list[Check] = []
    v = sys.version_info
    out.append(Check("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"
                     + ("" if v >= (3, 12) else ": Ledger needs 3.12 or later")))
    git = shutil.which("git")
    if git:
        try:
            ver = subprocess.run([git, "--version"], capture_output=True, text=True,
                                 timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            ver = git
        out.append(Check("git", True, ver))
    else:
        out.append(Check("git", False, "not found on PATH: install git"))
    mcp = importlib.util.find_spec("mcp") is not None
    out.append(Check("mcp extra", mcp,
                     "installed" if mcp else
                     "missing: the MCP server will not start. Reinstall with the [mcp] extra",
                     required=False))
    try:
        path = Config.resolve_path(config_path)
        exists = Path(path).is_file()
    except (FileNotFoundError, OSError):
        path, exists = None, False
    out.append(Check("config", exists, str(path) if exists else
                     "none found: run `aglium-ledger init` in one of your organisation's checkouts"))
    return out


@dataclass
class RepoSeen:
    repo: str
    default_branch: str
    last_commit: date | None = None
    pull_requests: int = 0          # merged in the window, by merge commit or squash
    other_commits: int = 0          # with no pull request in them
    fetched_days_ago: int | None = None   # for a mirror: when it was last fetched
    error: str | None = None


@dataclass
class Coverage:
    days: int
    repos: list[RepoSeen] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    #: Repositories the forge lists that are not on this machine; None when not asked.
    not_here: list[str] | None = None
    forge_error: str | None = None
    roots: list[str] = field(default_factory=list)

    @property
    def readable(self) -> list[RepoSeen]:
        return [r for r in self.repos if r.error is None]

    @property
    def replayable(self) -> list[RepoSeen]:
        return [r for r in self.readable if r.pull_requests]

    def gaps(self) -> list[str]:
        out: list[str] = []
        if len(self.readable) < 2:
            out.append(f"only {len(self.readable)} readable repository: Ledger looks for effects "
                       "in other repositories, so it needs at least two. Run `aglium-ledger "
                       "mirrors`, or point `local_root` at the directory holding your clones.")
        for r in self.repos:
            if r.error:
                out.append(f"{r.repo}: unreadable ({r.error})")
        if self.skipped:
            out.append("not readable as repositories: " + ", ".join(self.skipped))
        stale = [r.repo for r in self.readable if (r.fetched_days_ago or 0) > 7]
        if stale:
            out.append(f"{len(stale)} mirror(s) not fetched for over a week, so recent work there "
                       "is invisible: " + ", ".join(stale[:10]) + (" …" if len(stale) > 10 else "")
                       + ". Run `aglium-ledger mirrors`.")
        quiet = [r.repo for r in self.readable if not r.pull_requests and r.other_commits]
        if quiet:
            out.append("commits but no pull requests in the window (rebased or pushed "
                       "directly), so nothing to replay: " + ", ".join(quiet[:10]))
        if self.not_here:
            out.append(f"{len(self.not_here)} repositor{'y' if len(self.not_here) == 1 else 'ies'} "
                       "in your organisation are not on this machine and are invisible to every "
                       "answer: " + ", ".join(self.not_here[:15])
                       + (" …" if len(self.not_here) > 15 else "")
                       + ". `aglium-ledger mirrors` fetches them.")
        if self.forge_error:
            out.append(f"could not list the organisation on its forge: {self.forge_error}")
        return out

    def verdict(self) -> str:
        """One line: can a replay say anything here, and if not, why not."""
        if len(self.readable) < 2:
            return "not enough coverage: fewer than two repositories are readable"
        if not self.replayable:
            return (f"not enough history: no pull request merged in the last {self.days} days in "
                    f"any of {len(self.readable)} repositories")
        return (f"ready to replay: {len(self.readable)} repositories readable, "
                f"{sum(r.pull_requests for r in self.readable)} pull requests merged in "
                f"{len(self.replayable)} of them in the last {self.days} days")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict(), "days": self.days, "roots": self.roots,
            "repos": [{
                "repo": r.repo, "default_branch": r.default_branch,
                "last_commit": r.last_commit.isoformat() if r.last_commit else None,
                "pull_requests": r.pull_requests, "other_commits": r.other_commits,
                "fetched_days_ago": r.fetched_days_ago, "error": r.error,
            } for r in self.repos],
            "skipped": self.skipped, "excluded": self.excluded, "not_here": self.not_here,
            "gaps": self.gaps(),
        }


def _fetched_days_ago(p: Path) -> int | None:
    """How long since a mirror was last fetched: git touches FETCH_HEAD each time."""
    for f in (p / "FETCH_HEAD", p / ".git" / "FETCH_HEAD"):
        try:
            return int((time.time() - f.stat().st_mtime) // 86400)
        except OSError:
            continue
    return None


def survey(cfg: Config, vcs, *, days: int = 90, forge: list | None = None) -> Coverage:
    """Every repository `vcs` can read, with its history in the last `days`.

    `forge`, given, is the forge adapters to list the organisation with, so
    repositories that exist but are not on this machine are named.
    """
    from ledger import replay as rp

    since = date.today() - timedelta(days=days)
    cov = Coverage(days=days, excluded=sorted(set(cfg.exclude_repos)),
                   roots=[str(r) for r in getattr(vcs, "roots", [])])
    for ref in vcs.list_repos():
        if ref.full_name in cfg.exclude_repos:
            continue
        seen = RepoSeen(ref.full_name, ref.default_branch)
        try:
            p = vcs._path(ref.full_name)
            resolved = vcs._resolve(p, ref.default_branch)
            last = rp._git(p, "log", "-1", "--date=short", "--format=%cd", resolved).strip()
            seen.last_commit = date.fromisoformat(last) if last else None
            _, h = rp._history(p, ref.full_name, resolved, cfg.project_keys, since)
            seen.pull_requests, seen.other_commits = h.merges + h.squashes, h.other
            if vcs._bare(p):
                seen.fetched_days_ago = _fetched_days_ago(p)
        except (ForgeUnavailable, ValueError) as e:
            seen.error = str(e)[:160]
        cov.repos.append(seen)
    cov.skipped = list(getattr(vcs, "skipped", []))
    if forge is not None:
        if not forge:
            cov.forge_error = "no forge credential (set the token variable, or let git's credential helper answer)"
        else:
            from ledger.pipeline.mirrors import discover
            remotes, found = discover(forge, exclude=tuple(cfg.exclude_repos))
            if found.failed:
                cov.forge_error = "; ".join(f"{s}: {w[:100]}" for s, w in found.failed)
            here = {r.repo for r in cov.repos} | {r.repo.rsplit("/", 1)[-1] for r in cov.repos}
            cov.not_here = sorted(r.name for r in remotes if r.name not in here
                                  and r.full_name not in here)
    return cov
