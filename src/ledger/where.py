"""Where a brief's leading items can be read on this machine.

A brief names the other side of a change -- a commit, a branch, a decision --
and asks the reader to check it. On a real estate (Skycop, 18 Oct - 8 Nov
2026) six of twenty review sessions stopped at "that repository is not checked
out here, so I couldn't verify it", while every one of those repositories sat
in the ledger's own mirrors. The item said what to read, never where.

So each leading item gets one command that shows it: `git -C <dir> show <sha>`
and the like, against a checkout when one holds it, else the mirror. When the
repository or the commit is not on this machine, the line says that instead,
so nobody goes looking.

Computed by the client at render time, never by the server: the server's
paths are not the developer's. Everything here fails quietly -- an item with
no command is shown exactly as before.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from ledger.config import Config


def roots_of(cfg: Config | None) -> list[Path]:
    """Checkouts first, then mirrors: a checkout carries the developer's own
    fetches; the mirrors supply everything nobody checked out."""
    if cfg is None:
        return []
    out = []
    for r in (cfg.local_root, cfg.mirror_root):
        if r:
            p = Path(r).expanduser()
            if p.is_dir():
                out.append(p)
    return out


def _git(d: Path, *args: str) -> str | None:
    try:
        got = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return got.stdout.strip() if got.returncode == 0 else None


def _bare(d: Path) -> bool:
    return (d / "HEAD").is_file() and (d / "objects").is_dir() and (d / "refs").is_dir()


def candidates(repo: str, roots: list[Path]) -> list[Path]:
    """Directories holding `repo`, in the order they should be read.

    Only the two names a repository goes by on disk (`repo`, `repo.git`): a
    checkout under another directory name is found through the mirror instead,
    which keeps this to a few stat calls inside a hook.
    """
    name = repo.rsplit("/", 1)[-1]
    out = []
    for root in roots:
        for c in (root / name, root / f"{name}.git"):
            if (c / ".git").is_dir() or _bare(c):
                out.append(c)
    return out


def _cmd(d: Path, *args: str) -> str:
    return " ".join(["git", "-C", shlex.quote(str(d)), *(shlex.quote(a) for a in args)])


def _has(d: Path, rev: str) -> bool:
    return _git(d, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}") is not None


def _branch(d: Path, ref: str) -> str:
    """A branch as this layout names it: a mirror owns its branches, a
    checkout tracks them under origin."""
    return ref if _bare(d) else f"origin/{ref}"


def _default(d: Path) -> str | None:
    if _bare(d):
        return _git(d, "symbolic-ref", "--short", "HEAD")
    got = _git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if got:
        return got
    for b in ("origin/main", "origin/master"):
        if _has(d, b):
            return b
    return None


def _fetch_first(d: Path) -> str:
    return _cmd(d, "fetch") if _bare(d) else _cmd(d, "fetch", "origin")


def fetch_line(kind: str, src: dict, roots: list[Path]) -> str | None:
    """One line saying how to read this item here, or None when there is
    nothing useful to say (no roots configured, a kind with no single place)."""
    if not roots or kind not in ("impact", "decision"):
        return None
    repo = src.get("repo") or ""
    dirs = candidates(repo, roots)
    if not dirs:
        return (f"{repo} is not on this machine (looked in "
                + ", ".join(str(r) for r in roots) + "); ask its author or read it on the forge")
    if kind == "impact":
        sha, ref = src.get("sha"), src.get("ref") or ""
        if ref and not src.get("where"):
            # An open branch: its whole diff, not only the tip commit `sha` names.
            for d in dirs:
                b = _branch(d, ref)
                base = _default(d) if _has(d, b) else None
                if base:
                    return _cmd(d, "diff", f"{base}...{b}")
        if sha:
            for d in dirs:
                if _has(d, sha):
                    return _cmd(d, "show", sha)
            return f"{sha} is not in {dirs[0]} yet: {_fetch_first(dirs[0])}, then {_cmd(dirs[0], 'show', sha)}"
        for d in dirs:
            b = _branch(d, ref)
            if _has(d, b):
                base = _default(d)
                return _cmd(d, "diff", f"{base}...{b}") if base else _cmd(d, "log", "-p", "-5", b)
        return f"branch {ref} is not in {dirs[0]} yet: {_fetch_first(dirs[0])}, then read it there"
    path = src.get("path") or ""
    for d in dirs:
        rev = _branch(d, src["ref"]) if src.get("ref") else (_default(d) if not _bare(d) else "HEAD")
        if rev and _has(d, rev):
            return _cmd(d, "show", f"{rev}:{path}")
    return None


def annotate(b: dict, roots: list[Path]) -> dict:
    """Add `fetch` to each leading item of a brief payload, in place.

    Only the lead: it is what the reader is told to check, and at most a few
    items, so this stays a handful of git calls.
    """
    try:
        lead = (b.get("ranked") or {}).get("lead") or []
        if not lead or not roots:
            return b
        from ledger.core.rank import decision_key, impact_key
        by_key: dict[str, tuple[str, dict]] = {}
        for i in b.get("impacts", []):
            by_key[impact_key(i)] = ("impact", i)
        for d in b.get("decisions", []):
            by_key[decision_key(d)] = ("decision", d)
        for item in lead:
            kind, src = by_key.get(item.get("key"), (None, None))
            if kind and not item.get("fetch"):
                line = fetch_line(kind, src, roots)
                if line:
                    item["fetch"] = line
    except Exception:                               # noqa: BLE001 -- never cost a brief
        pass
    return b
