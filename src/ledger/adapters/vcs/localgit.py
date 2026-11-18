"""Local-git adapter: reads clones on disk instead of a forge API.

Not in the original adapter list. Added because P0-ticket's verification step
needs real data and this runs with zero credentials against clones that already
exist. It is also a fourth implementation for the shared contract suite, which
is the cheapest possible way to prove the port is not GitHub-shaped by accident.

Read-only: `upsert_comment` is deliberately unsupported rather than faked.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .base import (
    BranchRef, ChangedFile, CommitRef, ForgeUnavailable, PullRef, RepoRef, VcsAdapter,
)


def repo_name_hint(p: Path) -> str:
    return p.name


def origin_name(p: Path) -> str | None:
    """A repository's name where it lives, from its origin remote.

    A directory name is whatever the person who cloned it typed: a real
    estate had `acme-orchestrator-service` checked out as
    `orcestrator_service`, typo and all. Identity has to come from the remote,
    or the same repository is two different things depending on whose laptop
    is being read -- and a hook resolving the directory name finds nothing in
    an index built from the organisation.
    """
    try:
        r = subprocess.run(["git", "-C", str(p), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    url = r.stdout.strip()
    if r.returncode != 0 or not url:
        return None
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name or None


#: How long a repository's open pull requests are reused. The server syncs
#: every few minutes over ~180 repositories; asking each forge every time is
#: more calls an hour than Bitbucket allows, and a pull request opened ten
#: minutes ago is not what a brief misses.
PULLS_TTL = 900.0
_PULLS: dict[tuple[str, str], tuple[float, list]] = {}
_PULLS_LOCK = __import__("threading").Lock()

_ORIGIN = re.compile(r"^(?:[a-z+]+://(?:[^@/]+@)?(?P<h1>[^/:]+)(?::\d+)?/|[^@]+@(?P<h2>[^:]+):)"
                     r"(?P<path>[^?#]+?)(?:\.git)?/?$")


def forge_name(p: Path) -> tuple[str, str] | None:
    """`(host, "Org/name")` of a repository's origin, or None.

    `https://github.com/Acme/orders.git` and `git@github.com:Acme/orders.git`
    are both `("github.com", "Acme/orders")`.
    """
    try:
        r = subprocess.run(["git", "-C", str(p), "config", "--get", "remote.origin.url"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _ORIGIN.match(r.stdout.strip()) if r.returncode == 0 else None
    if not m or m.group("path").count("/") != 1:
        return None
    return (m.group("h1") or m.group("h2")).lower(), m.group("path")


def _serves(forge, host: str, full_name: str) -> bool:
    """Is this forge adapter the one for that origin? By organisation, then host."""
    ns = getattr(forge, "org", None) or getattr(forge, "workspace", None)
    if not ns or full_name.split("/", 1)[0].lower() != str(ns).lower():
        return False
    kind = {"github": "github", "bitbucket": "bitbucket"}.get(getattr(forge, "source", ""), "")
    return not kind or kind in host or getattr(forge, "host", "") == host


def _cached_pulls(forge, full_name: str) -> list:
    """`forge.list_open_pulls`, reused for `PULLS_TTL`. A forge that fails
    falls back to the last answer it gave; with none, the failure stands."""
    import time
    key = (getattr(forge, "source", type(forge).__name__), full_name)
    now = time.monotonic()
    with _PULLS_LOCK:
        hit = _PULLS.get(key)
    if hit and now - hit[0] < PULLS_TTL:
        return hit[1]
    try:
        got = forge.list_open_pulls(full_name)
    except ForgeUnavailable:
        if hit:
            return hit[1]
        raise
    with _PULLS_LOCK:
        _PULLS[key] = (now, got)
    return got


def _git(repo: Path, *args: str, timeout: float = 30, env: dict | None = None) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ForgeUnavailable("localgit", str(e)) from e
    if r.returncode != 0:
        raise ForgeUnavailable("localgit", f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return r.stdout


#: Past this many commits of its own that another change lacks, one change is
#: not a copy of another; reading their patches would only cost.
_CONTAINED_MAX = 200
#: Files a patch comparison reads `outer`'s history for, at most.
_CONTAINED_PATHS = 200


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
                       capture_output=True, timeout=30)
    return r.returncode == 0


def _read_blobs(p: Path, rev: str, paths: list[str], source: str = "localgit") -> dict[str, str]:
    """Files at `rev`, read through one `git cat-file --batch`; binary and non-UTF-8 left out."""
    if not paths:
        return {}
    request = "".join(f"{rev}:{path}\n" for path in paths).encode()
    try:
        r = subprocess.run(["git", "-C", str(p), "cat-file", "--batch"],
                           input=request, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ForgeUnavailable(source, str(e)) from e
    if r.returncode != 0:
        raise ForgeUnavailable(source, f"cat-file --batch failed in {p.name}")
    out: dict[str, str] = {}
    data, pos = r.stdout, 0
    for path in paths:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].decode(errors="replace")
        pos = nl + 1
        if header.endswith(" missing"):
            continue
        try:
            _, _, size = header.rsplit(" ", 2)
            size = int(size)
        except ValueError:
            break
        blob = data[pos:pos + size]
        pos += size + 1            # payload is followed by a newline
        try:
            out[path] = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return out


#: A line with a word or number in it; `}` or `),` alone says nothing.
_SAYS = re.compile(r"\w")
#: Answers by commit: what two commits are to each other never changes. A run
#: over every open change asks of each pair from both ends, and every brief of
#: a port asked the same of its own large diff once per candidate.
_BY_COMMITS: dict[tuple, object] = {}
_BY_COMMITS_MAX = 20_000
#: The diffs and file reads `_hunks_in` repeats for one change, by commit.
#: Large: kept to a few.
_TEXTS: dict[tuple, object] = {}
_TEXTS_MAX = 64
#: Files `_hunks_in` compares, at most; past it the patches decide.
_HUNKS_PATHS = 60


def _remember(memo: dict, limit: int, key: tuple, value):
    if len(memo) >= limit:
        memo.clear()
    memo[key] = value
    return value


#: Files `work_landed` reads `into`'s history of, at most; past it, it cannot say.
_LANDED_PATHS = 60
#: Shorter lines (`return x;`) are in every history: they prove nothing landed.
_LANDED_LINE = 8


def _diff_lines(raw: str, sign: str) -> dict[str, list[list[str]]]:
    """Per file, the blocks of lines a `--unified=0` diff or log adds (`+`) or removes (`-`)."""
    out: dict[str, list[list[str]]] = {}
    path, block = None, []

    def close():
        if path is not None and block:
            out.setdefault(path, []).append(list(block))
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            close()
            path, block = line.split(" b/", 1)[-1], []
        elif line.startswith("@@"):
            close()
            block = []
        elif line.startswith(sign) and not line.startswith(sign * 3):
            block.append(line[1:])
    close()
    return out


def _blocks_in(lines: list[str], block: tuple[str, ...]) -> int:
    """How many times `block` occurs in `lines` as consecutive whole lines."""
    n = len(block)
    if not n:
        return 0
    first = block[0]
    return sum(1 for k in range(len(lines) - n + 1)
               if lines[k] == first and tuple(lines[k:k + n]) == block)


#: Whether this git has `merge-tree --write-tree` (2.38+); None until asked.
_MERGE_TREE: bool | None = None


def _merges_to_itself(repo: Path, outer: str, inner: str) -> bool | None:
    """Whether merging `inner` into `outer` leaves `outer`'s tree as it is.

    True: it would change nothing. False: it merges cleanly into a different
    tree, so `inner` has something `outer` lacks. None: a conflict, or a git
    too old to merge without a working copy -- it cannot say.
    """
    global _MERGE_TREE
    if _MERGE_TREE is False:
        return None
    try:
        r = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", "--no-messages",
                            outer, inner], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in (0, 1):
        if "write-tree" in r.stderr or "usage" in r.stderr.lower():
            _MERGE_TREE = False         # an older git: never ask again
        return None
    _MERGE_TREE = True
    if r.returncode != 0:
        return None                     # a conflict: the line blocks decide
    merged = (r.stdout.split() or [""])[0]
    try:
        return merged == _git(repo, "rev-parse", f"{outer}^{{tree}}").strip()
    except ForgeUnavailable:
        return None


def _patch_ids(repo: Path, args: list[str]) -> set[str]:
    """`git patch-id --stable` of the patches a `git log -p` or `git show` prints."""
    try:
        log = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60)
        if log.returncode != 0:
            raise ForgeUnavailable("localgit", f"git {args[0]} failed")
        ids = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=log.stdout,
                             capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ForgeUnavailable("localgit", str(e)) from e
    return {line.split()[0] for line in ids.stdout.decode("utf-8", "replace").splitlines() if line}


def _split_diff(diff: str) -> dict[str, str]:
    """A unified diff, split into one body per file path."""
    files: dict[str, str] = {}
    path, body = None, []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if path:
                files[path] = "\n".join(body)
            path, body = line.split(" b/")[-1].strip(), []
        elif path is not None:
            body.append(line)
    if path:
        files[path] = "\n".join(body)
    return files


def _as_utc(value) -> datetime | None:
    """A date, a datetime or an ISO string, as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)



#: A hunk header without the function heading git appends to it: that is
#: text from the file, not from the change, and would read as changed.
_HUNK_HEAD = re.compile(r"^(@@ [^@]* @@).*$")

class LocalGitAdapter(VcsAdapter):
    source = "localgit"
    supports_comments = False
    supports_diffs = True
    supports_tree = True

    def __init__(self, root: str | Path | list, *, pulls_from: list | None = None):
        """One or more directories holding repositories.

        With several, the first that has a repository wins. That is how a
        developer's own checkouts sit in front of the server's mirrors: the
        checkout has a working tree, so uncommitted work is visible, and the
        mirrors supply every repository nobody checked out.

        `pulls_from`: forge adapters that answer the one question git cannot
        -- which pull requests are open. Code still comes from disk.
        """
        self._pulls_from = list(pulls_from or [])
        roots = root if isinstance(root, (list, tuple)) else [root]
        self.roots = [Path(r).expanduser() for r in roots]
        self.roots = [r for r in self.roots if r.is_dir()]
        if not self.roots:
            raise ForgeUnavailable(self.source, f"{root} is not a directory")
        self.root = self.roots[0]
        #: Repos we could not characterise. Visible, never silent.
        self.skipped: list[str] = []
        #: Branch names per repo, for _resolve. Only ever used to pick which
        #: spelling of a ref exists; a miss falls back to asking git.
        self._branch_names: dict[Path, frozenset[str]] = {}
        #: Repository name -> directory, built only when a name does not match
        #: a directory. Mirrors are named after their repository, so this stays
        #: empty for a server and is paid for only by a developer's clones.
        self._by_name: dict[str, Path] | None = None
        #: The listing is a snapshot per instance, like the branch names: one
        #: command sees one estate. A brief asked for it twice -- once to scope
        #: itself and once inside subject resolution -- and on 171 mirrors that
        #: was 3.7 seconds each time.
        self._repos: list[RepoRef] | None = None
        #: refname -> sha per repo, read from disk. The stored index checks
        #: every repository's head on every query, to catch one that moved
        #: since the last sync; on 171 mirrors that was 171 subprocesses and
        #: 3.4 seconds of a 6 second answer. Git keeps the same answer in
        #: `packed-refs` and a handful of loose files.
        self._ref_shas: dict[Path, dict[str, str]] = {}

    @staticmethod
    def _bare(d: Path) -> bool:
        """A bare repository: the repository IS the git directory.

        This is what a server holds. Mirrors are how the estate gets onto a
        machine without anybody checking anything out -- no working trees, a
        fifth of the disk, and every read the index needs still works.
        """
        return (d / "HEAD").is_file() and (d / "objects").is_dir() and (d / "refs").is_dir()

    def _path(self, repo: str) -> Path:
        for root in self.roots:
            for candidate in (root / repo, root / f"{repo}.git"):
                if (candidate / ".git").is_dir() or self._bare(candidate):
                    return candidate
        if self._by_name is None:
            self._by_name = {r.full_name: Path(r.url) for r in self.list_repos() if r.url}
        found = self._by_name.get(repo)
        if found is not None:
            return found
        # Caught by the shared contract suite: returning empty for a repo
        # that is gone makes "deleted" indistinguishable from "nothing here".
        raise ForgeUnavailable(self.source, f"{repo} is not a git clone under {self.root}")

    def _branch_ref_space(self, p: Path) -> str:
        """Where this layout keeps branches.

        A working clone tracks them under `refs/remotes/origin`; a mirror owns
        them outright under `refs/heads`. Reading the wrong one lists nothing,
        which looks exactly like a repository with no branches.
        """
        return "refs/heads" if self._bare(p) else "refs/remotes/origin"

    @staticmethod
    def _is_worktree(d: Path) -> bool:
        """A git worktree has `.git` as a FILE pointing at the parent's gitdir.

        Worktrees must be excluded, not merely handled: a worktree is the SAME
        repository checked out twice, so indexing it double-counts every branch
        and makes identical files look like a fork that drifted -- which is how
        one early reading mistook a second checkout for an independent copy.
        """
        g = d / ".git"
        return g.is_file()

    def list_repos(self) -> list[RepoRef]:
        if self._repos is not None:
            return list(self._repos)
        out = []
        for d in sorted(d for root in self.roots for d in root.iterdir()):
            if not d.is_dir():
                continue
            g = d / ".git"
            if self._bare(d):
                # `orders.git` is the repository `orders`.
                name = d.name[:-4] if d.name.endswith(".git") else d.name
                if not _git(d, "for-each-ref", "--count=1").strip():
                    # An empty repository, not a broken one: the organisation
                    # created it and nobody has pushed. Nothing to index and
                    # nothing wrong.
                    self.skipped.append(f"{name} (empty)")
                    continue
                try:
                    default = self._default_branch(d)
                except ForgeUnavailable:
                    self.skipped.append(d.name)
                    continue
                out.append(RepoRef(full_name=name, default_branch=default, host="local", url=str(d)))
                continue
            if not g.exists():
                continue
            if self._is_worktree(d):
                self.skipped.append(f"{d.name} (worktree)")
                continue
            if not g.is_dir():
                continue
            try:
                default = self._default_branch(d)
            except ForgeUnavailable:
                # One uncharacterisable repo must not take out the whole listing.
                # It is recorded and skipped, not allowed to fail the scan.
                self.skipped.append(d.name)
                continue
            out.append(RepoRef(full_name=origin_name(d) or d.name, default_branch=default,
                               host="local", url=str(d)))
        # First root wins: a checkout in front of a mirror of the same thing.
        seen: dict[str, RepoRef] = {}
        for r in out:
            order = next(i for i, root in enumerate(self.roots) if str(r.url).startswith(str(root)))
            if r.full_name not in seen or order < seen[r.full_name][0]:
                seen[r.full_name] = (order, r)
        out = [r for _, r in sorted(seen.values(), key=lambda p: p[1].full_name)]
        self._repos = out
        return list(out)

    def list_branches(self, repo: str, *, since: datetime | None = None) -> list[BranchRef]:
        p = self._path(repo)
        space = self._branch_ref_space(p)
        raw = _git(
            p,
            # Full refname, not :short -- short renders refs/remotes/origin/HEAD
            # as "origin", which listed a phantom branch of that name in every repo.
            "for-each-ref",
            "--format=%(refname)%09%(objectname)%09%(committerdate:iso-strict)%09%(authoremail)",
            space,
        )
        out = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name, sha, ts = parts[0], parts[1], parts[2]
            # `<someone@example.com>`, angle brackets and all.
            who = (parts[3] if len(parts) > 3 else "").strip().strip("<>") or None
            name = name.removeprefix(space + "/")
            if name == "HEAD":
                continue
            try:
                when = datetime.fromisoformat(ts).astimezone(timezone.utc)
            except ValueError:
                when = None
            if since is not None and when is not None and when < since:
                continue
            out.append(BranchRef(repo=repo, name=name, head_sha=sha, updated_at=when,
                                 author=who))
        return out

    def fetch(self, exclude: tuple[str, ...] = ()) -> list[tuple[str, str]]:
        """`git fetch --prune origin` in every clone. Returns (repo, error) failures.

        A local clone only knows what was last fetched into it, so a long-running
        ledger over local clones is only as current as this. It touches remote
        refs only -- never a working tree, never a local branch.
        """
        from concurrent.futures import ThreadPoolExecutor

        # A prompt must fail at once rather than hang a sync loop nobody is
        # watching. The askpass helper is used only when there is a secret for
        # it to hand over: pointing git at one that answers nothing breaks the
        # fetch that the machine's own credential helper would have completed.
        helper = self.root / ".ledger-askpass"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "SSH_ASKPASS": ""}
        if os.environ.get("LEDGER_GIT_SECRET") and helper.is_file():
            env["GIT_ASKPASS"] = str(helper)

        def one(name: str) -> tuple[str, str] | None:
            # Unattended: a credential prompt must fail at once, not hang the
            # sync loop waiting for a terminal nobody is watching.
            try:
                _git(self._path(name), "fetch", "--prune", "--quiet", "origin", timeout=120, env=env)
            except ForgeUnavailable as e:
                return name, str(e)
            return None

        names = [r.full_name for r in self.list_repos() if r.full_name not in exclude]
        # Network-bound, so parallel. Eight keeps a forge's rate limits comfortable.
        with ThreadPoolExecutor(max_workers=8) as pool:
            failed = [f for f in pool.map(one, names) if f]
        self.refresh()
        return failed

    def list_open_pulls(self, repo: str) -> list[PullRef]:
        """Open pull requests, from the forge the repository's origin is on.

        Git carries no pull-request state, and a mirror-only server used to
        answer empty: on a real estate 140 of 242 open pull requests were not
        in flight at all, because only branches with a commit in the last 30
        days were, and a review waiting since September has none. So the list
        comes from the forge's API -- one paged call per repository, cached
        for `PULLS_TTL` -- while everything else is still read from disk.

        With no forge for this origin it is still empty, and that is the
        truthful answer, not a degraded one. The path check runs either way,
        so a missing repo raises rather than reading empty.
        """
        p = self._path(repo)
        if not self._pulls_from:
            return []
        where = forge_name(p)
        if where is None:
            return []
        host, full_name = where
        forge = next((f for f in self._pulls_from if _serves(f, host, full_name)), None)
        if forge is None:
            return []
        import dataclasses
        return [dataclasses.replace(pr, repo=repo) for pr in _cached_pulls(forge, full_name)]

    def upsert_comment(self, repo: str, pull_number: int, marker: str, body: str) -> str:
        raise ForgeUnavailable(self.source, "local clones have no pull requests to comment on")

    def _default_branch(self, p: Path) -> str:
        """origin/HEAD when it is set, otherwise probe for a ref that exists.

        Falling back to a hardcoded "master" is a guess, and a wrong guess here
        is silent and total: every tree read and every diff for that repo is
        computed against a ref that does not exist. A clone with origin/main
        and no origin/HEAD is exactly this case, and it is common.
        """
        # A mirror owns its HEAD and it is authoritative. A working clone's
        # HEAD is wherever the developer is standing: reading it made two
        # repositories that happened to be checked out on `stage` report
        # `stage` as their default branch, which turns the integration branch
        # into the baseline and makes everything merged into it look already
        # released.
        if self._bare(p):
            # One file read instead of a subprocess. Over 171 mirrors that is
            # the difference between four seconds and none, twice per brief.
            try:
                head = (p / "HEAD").read_text().strip()
            except OSError:
                head = ""
            if head.startswith("ref: refs/heads/"):
                return head.removeprefix("ref: refs/heads/")
        candidates = [("refs/remotes/origin/HEAD", "refs/remotes/origin/")]
        if self._bare(p):
            candidates.append(("HEAD", "refs/heads/"))
        for head, prefix in candidates:
            try:
                # The prefix, not the last segment: a branch name may contain
                # slashes, and `refs/heads/feat/x` is the branch `feat/x`.
                # Taking the last segment made a repository whose HEAD pointed
                # at `feat/x` report its default branch as `x`, which exists
                # nowhere, and every read of that repository then failed.
                return _git(p, "symbolic-ref", head).strip().removeprefix(prefix)
            except ForgeUnavailable:
                continue
        # origin/* first, then local heads: a clone with no remote still has a
        # default branch, and the ledger's own repo is exactly that case.
        for prefix in ("origin/", ""):
            for candidate in ("main", "master", "develop", "trunk"):
                r = subprocess.run(
                    ["git", "-C", str(p), "rev-parse", "--verify", "-q", f"{prefix}{candidate}^{{commit}}"],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    return candidate
        raise ForgeUnavailable(self.source, f"{p.name}: cannot determine a default branch")

    _COMMITISH = re.compile(r"^[0-9a-f]{7,40}$|[~^@:]|^HEAD$")

    def refresh(self) -> None:
        """Forget the snapshots. A long-running caller does this per cycle."""
        self._branch_names.clear()
        self._ref_shas.clear()
        self._repos = None
        self._by_name = None

    def _names(self, p: Path) -> frozenset[str]:
        """Branch names, in both spellings, from the same on-disk read as the
        shas. This was a `for-each-ref` per repository, which is the other
        subprocess the stored index paid for on every query."""
        if p not in self._branch_names:
            names = set()
            for refname in self._refs(p):
                if refname.startswith("refs/heads/"):
                    names.add(refname[len("refs/heads/"):])
                elif refname.startswith("refs/remotes/"):
                    names.add(refname[len("refs/remotes/"):])
            self._branch_names[p] = frozenset(names)
        return self._branch_names[p]

    def _resolve_base(self, p: Path, base: str, head: str) -> str:
        """A base branch, resolved to its freshest copy.

        In a working clone `stage` is whatever was last checked out, often
        weeks behind `origin/stage`. Diffing against the stale copy credits
        the change with every commit `stage` gained since -- the measured
        case: a three-file, 13-line change briefed as decision_needed because
        86 unreleased commits on `stage` were read as its own. Whichever copy
        leaves the fewest commits unique to the change is the one it is built
        on; a mirror has only one copy, and nothing changes there.
        """
        names = self._names(p)
        both = [c for c in (base, f"origin/{base}") if c in names]
        if len(both) < 2:
            return self._resolve(p, base)
        h = self._resolve(p, head)
        counts = {}
        for c in both:
            try:
                counts[c] = int(_git(p, "rev-list", "--count", f"{c}..{h}").strip() or 0)
            except (ForgeUnavailable, ValueError):
                continue
        return min(counts, key=lambda c: (counts[c], c != f"origin/{base}")) if counts \
            else self._resolve(p, base)

    def _resolve(self, p: Path, ref: str) -> str:
        """Branch names are listed with `origin/` stripped, but only the remote
        refs exist in a bare-ish clone. Resolve back to whichever one git has.

        Probing with `rev-parse` cost two processes per ref -- ~200 per brief,
        most of its runtime, and mostly spent confirming that `stage` does not
        exist in repos that have none. One `for-each-ref` per repo answers it.

        The branch list is a snapshot per adapter instance: one command, or one
        sync cycle, sees one consistent set of refs. `refresh()` drops it.
        Commit-ish strings (a sha, `HEAD~1`) are never branch names, so those
        are still asked of git directly.
        """
        names = self._names(p)
        for candidate in (ref, f"origin/{ref}"):
            if candidate in names:
                return candidate
        if names and not self._COMMITISH.search(ref):
            raise ForgeUnavailable(self.source, f"{repo_name_hint(p)}: cannot resolve ref {ref!r}")
        for candidate in (ref, f"origin/{ref}"):
            r = subprocess.run(["git", "-C", str(p), "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return candidate
        raise ForgeUnavailable(self.source, f"{repo_name_hint(p)}: cannot resolve ref {ref!r}")

    def changed_files(self, repo: str, base: str, head: str) -> list[ChangedFile]:
        return self._changed(repo, base, head, None)

    def changed_files_in(self, repo: str, base: str, head: str, paths: list[str]) -> list[ChangedFile]:
        return self._changed(repo, base, head, list(paths)) if paths else []

    def _changed(self, repo: str, base: str, head: str, paths: list[str] | None) -> list[ChangedFile]:
        p = self._path(repo)
        base, head = self._resolve_base(p, base, head), self._resolve(p, head)
        # merge-base, not a straight two-dot diff: comparing against the tip of
        # base attributes every commit that landed on base since the branch
        # forked to this change, which is somebody else's work.
        try:
            merge_base = _git(p, "merge-base", base, head).strip()
        except ForgeUnavailable:
            merge_base = base
        raw = _git(p, "diff", "--unified=0", f"{merge_base}..{head}", *(["--", *paths] if paths else []))
        out: list[ChangedFile] = []
        path, buf = None, []
        for line in raw.splitlines():
            if line.startswith("diff --git "):
                if path:
                    out.append(ChangedFile(path=path, body="\n".join(buf)))
                path, buf = line.split(" b/", 1)[-1], []
            elif path and (line.startswith(("+", "-", "@@")) and not line.startswith(("+++", "---"))):
                # Hunk headers stay: they say where a deleted line stood, and
                # a line from the middle of a docstring reads as code without.
                buf.append(_HUNK_HEAD.sub(r"\1", line) if line.startswith("@@") else line)
        if path:
            out.append(ChangedFile(path=path, body="\n".join(buf)))
        return out

    def grep_paths(self, repo: str, ref: str, needles: list[str], paths: list[str] | None = None) -> list[str]:
        needles = [n for n in needles if n]
        if not needles:
            return []
        p = self._path(repo)
        args = ["grep", "-l", "-i", "-F"]
        for n in needles:
            args += ["-e", n]
        args.append(self._resolve(p, ref))
        r = subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, timeout=60)
        if r.returncode not in (0, 1):             # 1 = no match
            raise ForgeUnavailable(self.source, f"{repo}: git grep failed: {r.stderr.strip()[:200]}")
        prefix = self._resolve(p, ref) + ":"
        found = [line[len(prefix):] if line.startswith(prefix) else line for line in r.stdout.splitlines()]
        return [f for f in found if paths is None or f in set(paths)]

    def working_changes(self, repo: str) -> list[ChangedFile] | None:
        """`git diff HEAD` plus untracked files, as hunks.

        A mirror has no working tree, so there is nothing uncommitted to see
        and it says so -- None, never an empty list, because "this source
        cannot tell" is not "nothing has changed".

        Untracked files are rendered as all-additions: a new module is part of
        the change, and reading it as nothing would miss exactly the contracts
        a new consumer introduces.
        """
        p = self._path(repo)
        if self._bare(p):
            return None
        try:
            tracked = subprocess.run(["git", "-C", str(p), "diff", "HEAD", "--unified=3"],
                                     capture_output=True, text=True, timeout=60)
            untracked = subprocess.run(["git", "-C", str(p), "ls-files", "--others",
                                        "--exclude-standard"],
                                       capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if tracked.returncode != 0:
            return None
        out = [ChangedFile(path=path, body=body) for path, body in _split_diff(tracked.stdout).items()]
        for path in untracked.stdout.splitlines()[:200]:
            if not path.strip():
                continue
            try:
                text = (p / path).read_text(errors="ignore")[:200_000]
            except OSError:
                continue
            out.append(ChangedFile(path=path, body="\n".join("+" + line for line in text.splitlines())))
        return out

    def commits_touching(self, repo: str, ref: str, name: str, since,
                         paths: list[str] | None = None) -> list[CommitRef] | None:
        """`git log -G`: commits whose diff changed a line naming this contract.

        Restricted to the paths the contract was found in, so the pickaxe runs
        over a handful of files rather than the whole history.

        `--since` is an optimisation here, not the filter. Git silently drops
        a date it cannot parse -- `--since=not-a-date` and `--since=2999-01-01`
        both return the entire history -- which would turn "five commits since
        the page was written" into "every commit ever" with no error. The
        cutoff is applied to the dates git reports instead, and a cutoff that
        cannot be read at all is answered with None.
        """
        cutoff = _as_utc(since)
        if not name or cutoff is None:
            return None if cutoff is None else []
        p = self._path(repo)
        args = ["log", f"--since={cutoff.isoformat()}", f"-G{re.escape(name)}",
                "--format=%H%x1f%cI%x1f%an%x1f%s", "-n", "50", self._resolve(p, ref)]
        if paths:
            args += ["--", *paths[:20]]
        try:
            r = subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            sha, when, author, subject = parts
            at = _as_utc(when)
            if at is None or at <= cutoff:
                continue
            out.append(CommitRef(sha=sha, subject=subject, author=author or None, when=at))
        return out

    def changed_paths(self, repo: str, base: str, head: str) -> list[str]:
        p = self._path(repo)
        base, head = self._resolve_base(p, base, head), self._resolve(p, head)
        try:
            merge_base = _git(p, "merge-base", base, head).strip()
        except ForgeUnavailable:
            merge_base = base
        return [line for line in _git(p, "diff", "--name-only", f"{merge_base}..{head}").splitlines()
                if line]

    def landing_merge(self, repo: str, into: str, head: str) -> str | None:
        p = self._path(repo)
        try:
            into_ref, tip = self._resolve(p, into), self._resolve(p, head)
            # `into`'s own line, newest first, past what `head` already has.
            # Not `--ancestry-path`: with `--first-parent` it walks only that
            # line, and a head one commit behind what was merged (the branch
            # moved on before its merge) has no path along it -- the merge
            # was never found.
            line = _git(p, "rev-list", "--first-parent", into_ref, f"^{tip}").split()
        except ForgeUnavailable:
            return None
        if not line or not _is_ancestor(p, tip, line[0]):
            return None
        # The oldest commit on the line that has `head`: once one has it, every
        # later one does, so the search halves.
        lo, hi = 0, len(line) - 1               # line[lo] has it; find the last that does
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _is_ancestor(p, tip, line[mid]):
                lo = mid
            else:
                hi = mid - 1
        merge = line[lo]
        # What the merge brought in includes this change: the head is behind
        # its second parent and not its first. A head already on `into`'s own
        # line (a fast-forward) has only later, unrelated merges after it.
        try:
            parents = _git(p, "rev-list", "--parents", "-n", "1", merge).split()[1:]
        except ForgeUnavailable:
            return None
        if len(parents) < 2 or _is_ancestor(p, tip, parents[0]) \
                or not _is_ancestor(p, tip, parents[1]):
            return None
        return merge

    def _shas(self, p: Path, *refs: str) -> tuple | None:
        """The commits `refs` stand for, cheaply; None when one cannot be read."""
        refs_on_disk = self._refs(p)
        out = []
        for ref in refs:
            sha = next((refs_on_disk[c] for c in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}")
                        if c in refs_on_disk), None)
            if sha is None:
                try:
                    sha = _git(p, "rev-parse", f"{self._resolve(p, ref)}^{{commit}}").strip()
                except ForgeUnavailable:
                    return None
            out.append(sha)
        return tuple(out)

    def _by_commits(self, name: str, repo: str, refs: tuple, compute):
        p = self._path(repo)
        shas = self._shas(p, *refs)
        key = (name, str(p), shas) if shas is not None else None
        if key is not None and key in _BY_COMMITS:
            return _BY_COMMITS[key]
        got = compute()
        if key is not None and got is not None:
            _remember(_BY_COMMITS, _BY_COMMITS_MAX, key, got)
        return got

    def adds_nothing(self, repo: str, into: str, head: str) -> bool | None:
        return self._by_commits("adds_nothing", repo, (into, head),
                                lambda: self._adds_nothing(repo, into, head))

    def work_landed(self, repo: str, into: str, inner: str, inner_base: str) -> bool | None:
        return self._by_commits("work_landed", repo, (into, inner, inner_base),
                                lambda: self._work_landed(repo, into, inner, inner_base))

    def work_contained(self, repo: str, outer: str, inner: str, inner_base: str) -> bool | None:
        return self._by_commits("work_contained", repo, (outer, inner, inner_base),
                                lambda: self._work_contained(repo, outer, inner, inner_base))

    def _adds_nothing(self, repo: str, into: str, head: str) -> bool | None:
        p = self._path(repo)
        try:
            into_ref, head_ref = self._resolve(p, into), self._resolve(p, head)
            if _git(p, "rev-list", "--no-merges", "-n", "1", head_ref, f"^{into_ref}").strip():
                return False        # a commit of its own: work, whatever its merges
        except ForgeUnavailable:
            return None
        return _merges_to_itself(p, into_ref, head_ref)

    def _work_landed(self, repo: str, into: str, inner: str, inner_base: str) -> bool | None:
        p = self._path(repo)
        try:
            into_ref, inner_ref = self._resolve(p, into), self._resolve(p, inner)
            base = self._resolve_base(p, inner_base, inner)
            if not _git(p, "rev-list", "--no-merges", "-n", "1", inner_ref, f"^{base}").strip():
                return None         # nothing of its own to have landed
            if not _git(p, "rev-list", "--no-merges", "-n", "1", inner_ref, f"^{base}",
                        f"^{into_ref}").strip():
                return True         # every commit of it is on `into`
            own = _git(p, "merge-base", base, inner_ref).strip()
            raw = _git(p, "diff", "--unified=0", "--no-renames", f"{own}..{inner_ref}", timeout=60)
            fork = _git(p, "merge-base", into_ref, inner_ref).strip()
        except ForgeUnavailable:
            return None
        added, removed = _diff_lines(raw, "+"), _diff_lines(raw, "-")
        paths = sorted(set(added) | set(removed))
        if not paths or len(paths) > _LANDED_PATHS:
            return None
        try:
            log = _git(p, "log", "-p", "--unified=0", "--no-renames", "--format=",
                       f"{fork}..{into_ref}", "--", *paths, timeout=90)
        except ForgeUnavailable:
            return None
        if not log.strip():
            return False
        def lines_of(found):
            return {path: {x.strip() for b in blocks for x in b} for path, blocks in found.items()}
        did_add, did_remove = lines_of(_diff_lines(log, "+")), lines_of(_diff_lines(log, "-"))
        checked = 0
        for want, did in ((added, did_add), (removed, did_remove)):
            for path, blocks in want.items():
                have = did.get(path, set())
                for b in blocks:
                    for x in b:
                        x = x.strip()
                        if len(x) < _LANDED_LINE or not _SAYS.search(x):
                            continue
                        if x not in have:
                            return False
                        checked += 1
        return checked > 0

    def _work_contained(self, repo: str, outer: str, inner: str, inner_base: str) -> bool | None:
        p = self._path(repo)
        try:
            outer_ref, inner_ref = self._resolve(p, outer), self._resolve(p, inner)
            base = self._resolve_base(p, inner_base, inner)
            if not _git(p, "rev-list", "--no-merges", "-n", "1", inner_ref, f"^{base}").strip():
                return False        # nothing of its own: there is no work to be a copy of
            # Its own commits that `outer` does not have by ancestry.
            missing = _git(p, "rev-list", "--no-merges", inner_ref, f"^{base}",
                           f"^{outer_ref}").split()
            if not missing:
                return True
            # Merged into `outer`, it would change nothing: its work is there
            # already, whatever the commits -- a squash of it, a port that
            # gathered it into one commit.
            # The line blocks only where the merge cannot say: a clean merge
            # into another tree is an answer, and costs one process, not ten.
            merged = _merges_to_itself(p, outer_ref, inner_ref)
            if merged or (merged is None and self._hunks_in(p, outer_ref, inner_ref, base)):
                return True
            if len(missing) > _CONTAINED_MAX:
                return False
            paths = [x for x in _git(p, "diff", "--name-only", f"{base}...{inner_ref}").splitlines() if x]
            fork = _git(p, "merge-base", outer_ref, inner_ref).strip()
            # What `outer` changed of those files since the two parted, as patches.
            theirs = _patch_ids(p, ["log", "-p", "--no-merges", "--format=commit %H",
                                    f"{fork}..{outer_ref}", "--", *paths[:_CONTAINED_PATHS]])
            if not theirs:
                return False
            ours = _patch_ids(p, ["show", "-p", "--format=commit %H", *missing])
        except ForgeUnavailable:
            return None
        return bool(ours) and ours <= theirs

    def _hunks_in(self, p: Path, outer: str, inner: str, base: str) -> bool:
        """Whether `outer`'s files already hold what `inner` does over `base`.

        A squash or a port carries the same lines under other commits; when
        `outer` then edits next to them, a three-way merge calls it a conflict
        although nothing of `inner` is missing. Every block of whole lines
        `inner` adds is in `outer` as often as in `inner`, and every line it
        removes is in `outer` no more often than in `inner` -- `timeout = 30`
        is not inside `timeout = 300`, and a branch that mostly deletes is
        not there until the deletions are.
        """
        try:
            fork = _git(p, "merge-base", base, inner).strip()
            inner_sha = _git(p, "rev-parse", f"{inner}^{{commit}}").strip()
            key = ("diff", str(p), fork, inner_sha)
            raw = _TEXTS.get(key)
            if raw is None:
                raw = _remember(_TEXTS, _TEXTS_MAX, key,
                                _git(p, "diff", "--unified=0", "--no-renames", f"{fork}..{inner}"))
        except ForgeUnavailable:
            return False
        added: dict[str, list[tuple[str, ...]]] = {}
        removed: dict[str, list[tuple[str, ...]]] = {}
        path, plus, minus = None, [], []

        def close():
            if path is not None:
                if plus:
                    added.setdefault(path, []).append(tuple(plus))
                if minus:
                    removed.setdefault(path, []).append(tuple(minus))
        for line in raw.splitlines():
            if line.startswith("diff --git "):
                close()
                path, plus, minus = line.split(" b/", 1)[-1], [], []
            elif line.startswith("@@"):
                close()
                plus, minus = [], []
            elif line.startswith("+") and not line.startswith("+++"):
                plus.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                minus.append(line[1:])
        close()
        paths = sorted(set(added) | set(removed))
        if not paths or len(paths) > _HUNKS_PATHS:
            return False
        try:
            texts = _read_blobs(p, outer, paths, self.source)
            key = ("blobs", str(p), inner_sha, tuple(paths))
            theirs = _TEXTS.get(key)
            if theirs is None:
                theirs = _remember(_TEXTS, _TEXTS_MAX, key, _read_blobs(p, inner, paths, self.source))
        except ForgeUnavailable:
            return False
        outer_lines = {k: v.splitlines() for k, v in texts.items()}
        inner_lines = {k: v.splitlines() for k, v in theirs.items()}
        for path in paths:
            o, i = outer_lines.get(path), inner_lines.get(path)
            # As many times as `inner` has it: a `return None` it adds is not
            # "there" because the file had another one already.
            for b in added.get(path, ()):
                if o is None or i is None or _blocks_in(o, b) < _blocks_in(i, b):
                    return False
            # A removed line, one by one: removals next to an edit come as one
            # block with it, and that block is in neither file. Lines of only
            # brackets or blanks say nothing either way.
            for line in {x for b in removed.get(path, ()) for x in b if _SAYS.search(x)}:
                if (o.count(line) if o is not None else 0) > (i.count(line) if i is not None else 0):
                    return False
        return True

    def list_tree(self, repo: str, ref: str) -> list[str]:
        p = self._path(repo)
        raw = _git(p, "ls-tree", "-r", "--name-only", self._resolve(p, ref))
        return [line for line in raw.splitlines() if line]

    def read_file(self, repo: str, ref: str, path: str) -> str:
        p = self._path(repo)
        r = subprocess.run(["git", "-C", str(p), "show", f"{self._resolve(p, ref)}:{path}"],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            raise ForgeUnavailable(self.source, f"{repo}:{path} not readable at {ref}")
        try:
            return r.stdout.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ForgeUnavailable(self.source, f"{repo}:{path} is not utf-8 text") from e

    _MERGE_REF = re.compile(r"Merge pull request #\d+ from [^/]+/(?P<ref>\S+)")

    def commits_between(self, repo: str, base: str, head: str, *,
                        limit: int | None = None,
                        exclude: tuple[str, ...] = ()) -> list[CommitRef]:
        """Commits on `head` and not on `base`, message bodies included.

        Records are separated by `%x1e` rather than by newline, because the
        body is multi-line -- which is the point of reading it. A ticket key
        the branch name does not carry is often in a trailer (`Refs: XY-525`)
        or in a line of explanation, and on a real estate that is the
        difference between 29% of branches linking and 74%.
        """
        p = self._path(repo)
        args = ["log", "--format=%H%x1f%s%x1f%an%x1f%aI%x1f%b%x1e"]
        if limit:
            args.append(f"-{int(limit)}")
        rev = [self._resolve(p, head), "--not", self._resolve_base(p, base, head)]
        for ref in exclude:
            try:
                rev.append(self._resolve(p, ref))
            except ForgeUnavailable:
                continue                    # this repo has no such branch
        raw = _git(p, *args, *rev)
        out: list[CommitRef] = []
        for record in raw.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            parts = record.split("\x1f")
            if len(parts) < 4:
                continue
            sha, subject, author, when = parts[:4]
            body = parts[4] if len(parts) > 4 else ""
            m = self._MERGE_REF.search(subject)
            try:
                ts = datetime.fromisoformat(when).astimezone(timezone.utc)
            except ValueError:
                ts = None
            out.append(CommitRef(sha=sha, subject=subject, author=author or None,
                                 when=ts, merged_ref=m.group("ref") if m else None,
                                 body=body))
        return out

    def merged_into(self, repo: str, into: str, ref: str, default: str) -> bool:
        p = self._path(repo)
        tip = _git(p, "rev-parse", self._resolve(p, ref)).strip()
        into_ref = self._resolve(p, into)
        if not tip or tip == _git(p, "rev-parse", into_ref).strip():
            return False
        line = _git(p, "rev-list", "--first-parent", into_ref,
                    f"^{self._resolve_base(p, default, into)}").split()
        return tip not in set(line)

    def commits_ahead(self, repo: str, base: str, head: str) -> int:
        p = self._path(repo)
        out = _git(p, "rev-list", "--count",
                   f"{self._resolve_base(p, base, head)}..{self._resolve(p, head)}")
        return int(out.strip() or 0)

    def read_files(self, repo: str, ref: str, paths: list[str]) -> dict[str, str]:
        """One `git cat-file --batch` process for the whole set.

        The per-file path spawned a `git show` for every file -- ~2,500 processes
        for one estate-wide scan. Batching reads them through a single pipe.
        Binary and non-UTF-8 blobs are skipped, as read_file would reject them.
        """
        if not paths:
            return {}
        p = self._path(repo)
        return _read_blobs(p, self._resolve(p, ref), paths, self.source)

    def _git_dir(self, p: Path) -> Path:
        return p if self._bare(p) else p / ".git"

    def _refs(self, p: Path) -> dict[str, str]:
        """Every ref this repository has, from disk rather than from git.

        `packed-refs` holds them all in one file after a clone or a `gc`;
        anything written since sits in a loose file under `refs/`. Loose wins,
        because that is what git does. A symbolic loose ref (`ref: ...`) is
        skipped -- it is a pointer, not a commit.
        """
        if p in self._ref_shas:
            return self._ref_shas[p]
        g = self._git_dir(p)
        out: dict[str, str] = {}
        try:
            for line in (g / "packed-refs").read_text().splitlines():
                if not line or line[0] in "#^":
                    continue
                sha, _, name = line.partition(" ")
                if len(sha) == 40 and name:
                    out[name.strip()] = sha
        except OSError:
            pass
        refs = g / "refs"
        if refs.is_dir():
            for f in refs.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    body = f.read_text().strip()
                except OSError:
                    continue
                if len(body) == 40 and not body.startswith("ref:"):
                    out[f"refs/{f.relative_to(refs)}"] = body
        self._ref_shas[p] = out
        return out

    def head_sha(self, repo: str, ref: str) -> str:
        p = self._path(repo)
        refs = self._refs(p)
        for candidate in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}",
                          ref if ref.startswith("refs/") else None):
            if candidate and candidate in refs:
                return refs[candidate]
        # A sha, a tag, `HEAD~1`: only git can say.
        return _git(p, "rev-parse", f"{self._resolve(p, ref)}^{{commit}}").strip()
