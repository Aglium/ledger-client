"""The estate the server holds: discovered from the organisation, mirrored on disk.

An engineer clones the one repository they are working in. The server needs
all of them, and it has to find them itself -- from the organisation, through
the forge -- rather than inheriting whatever happens to be checked out on
somebody's laptop. A ledger that can only see the repositories a developer
already cloned answers questions about exactly the repositories that developer
did not need answered.

Mirrors rather than checkouts. `git clone --mirror` keeps the objects and the
refs and no working tree: about a fifth of the disk, cheaper to fetch, and
every read the index performs -- trees, diffs, `git log -G`, commits between a
default and an integration branch -- works unchanged. The two capabilities a
mirror cannot serve are the two that are nobody else's business anyway: a
developer's uncommitted files live on the developer's machine.

Credentials never reach the disk. The remote is stored without them and the
secret is handed to git through `GIT_ASKPASS` for the life of one command, so
it is not written into a config file and not visible in a process listing.
"""

from __future__ import annotations

import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from ledger.adapters.vcs.base import ForgeUnavailable, VcsAdapter

#: Written once into the mirror root. It carries no secret: it echoes one that
#: only exists in the environment of the command being run.
_ASKPASS = '#!/bin/sh\nprintf "%s" "$LEDGER_GIT_SECRET"\n'
_ASKPASS_NAME = ".ledger-askpass"


@dataclass(frozen=True)
class Remote:
    """One repository the organisation has."""

    name: str               # identity: the repository's own name, as people say it
    full_name: str          # as the forge knows it, organisation-qualified
    clone_url: str
    default_branch: str
    host: str
    source: str             # github | bitbucket


@dataclass
class MirrorResult:
    discovered: int = 0
    cloned: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: Two repositories in different organisations with the same name. Recorded
    #: rather than silently merged: they are not the same repository, and
    #: whichever one lost would be missing from every answer.
    collisions: list[str] = field(default_factory=list)
    #: Mirrors on disk that the organisation no longer lists.
    orphaned: list[str] = field(default_factory=list)


def _clone_url(full_name: str, host: str, url: str | None) -> str:
    if url and url.startswith(("http://", "https://")):
        return url if url.endswith(".git") else url + ".git"
    return f"https://{host}/{full_name}.git"


def discover(adapters: list[VcsAdapter], *, exclude: tuple[str, ...] = ()) -> tuple[list[Remote], MirrorResult]:
    """Every repository the configured organisations have.

    A forge that cannot be reached is reported, never treated as an
    organisation with no repositories -- that would quietly shrink the estate
    to whatever happened to answer.
    """
    res = MirrorResult()
    out: dict[str, Remote] = {}
    for adapter in adapters:
        try:
            repos = adapter.list_repos()
        except ForgeUnavailable as e:
            res.failed.append((adapter.source, str(e)))
            continue
        for r in repos:
            name = r.full_name.rsplit("/", 1)[-1]
            if name in exclude or r.full_name in exclude:
                continue
            if name in out and out[name].full_name != r.full_name:
                res.collisions.append(f"{out[name].full_name} and {r.full_name} are both {name!r}")
                continue
            out[name] = Remote(name=name, full_name=r.full_name,
                               clone_url=_clone_url(r.full_name, r.host, r.url),
                               default_branch=r.default_branch, host=r.host,
                               source=adapter.source)
    res.discovered = len(out)
    return sorted(out.values(), key=lambda r: r.name), res


def _askpass(root: Path) -> Path:
    p = root / _ASKPASS_NAME
    if not p.is_file():
        p.write_text(_ASKPASS)
    p.chmod(stat.S_IRWXU)
    return p


def _url_with_user(url: str, user: str | None) -> str:
    """Put the username in the URL so git only has to ask for the secret.

    The username is percent-encoded. Atlassian accounts are email addresses,
    and an unencoded `@` makes git read `example.com@bitbucket.org` as the
    hostname: every clone from that workspace failed with "Bad hostname".

    Only for http(s). An ssh remote carries its own user, and a filesystem
    path has nowhere to put one.
    """
    if not user or not url.startswith(("http://", "https://")):
        return url
    scheme, rest = url.split("//", 1)
    if "@" in rest.split("/", 1)[0]:
        return url
    return f"{scheme}//{quote(user, safe='')}@{rest}"


def sync(root: str | Path, remotes: list[Remote], *,
         credentials: dict[str, tuple[str, str]] | None = None,
         workers: int = 8, timeout: float = 600) -> MirrorResult:
    """Clone what is missing, fetch what is there. Returns what happened."""
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    helper = _askpass(root)
    credentials = credentials or {}
    res = MirrorResult(discovered=len(remotes))

    def one(r: Remote) -> None:
        user, secret = credentials.get(r.source, (None, None))
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if secret:
            env.update({"GIT_ASKPASS": str(helper), "LEDGER_GIT_SECRET": secret})
        # No secret: leave the machine's credential helper alone. Overriding
        # GIT_ASKPASS with one that answers nothing turns a configured machine
        # into one that cannot authenticate at all.
        path = root / f"{r.name}.git"
        if path.is_dir():
            args = ["git", "-C", str(path), "fetch", "--prune", "--quiet", "origin"]
            done = res.fetched
        else:
            args = ["git", "clone", "--mirror", "--quiet",
                    _url_with_user(r.clone_url, user), str(path)]
            done = res.cloned
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            res.failed.append((r.name, str(e)[:200]))
            return
        if p.returncode == 0 and r.default_branch:
            # HEAD in a fresh mirror follows the source. Point it at what the
            # organisation says is the default, so the index is not built
            # against whichever branch someone happened to have checked out.
            subprocess.run(["git", "-C", str(path), "symbolic-ref", "HEAD",
                            f"refs/heads/{r.default_branch}"],
                           capture_output=True, text=True, env=env)
        if p.returncode != 0:
            # The secret is never in the command, but a server can still echo a
            # URL back; keep the reason and drop anything that looks like one.
            why = " ".join(w for w in p.stderr.strip().split() if "@" not in w)[:200]
            res.failed.append((r.name, why or f"git exited {p.returncode}"))
            return
        done.append(r.name)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, remotes))

    known = {f"{r.name}.git" for r in remotes}
    res.orphaned = sorted(d.name for d in root.iterdir()
                          if d.is_dir() and d.name.endswith(".git") and d.name not in known)
    res.cloned.sort()
    res.fetched.sort()
    res.failed.sort()
    return res
