"""The operations the client offers, with no surface in them.

The CLI, the hooks and the MCP server are thin shells over these functions.
They raise instead of exiting, and return dataclasses or plain dicts instead of
printing, so the same answer comes out of every door.

Nothing here touches a database. A brief is computed against an *index* -- an
object answering what the rest of the estate holds -- and this package ships
the one that reads the estate live. The organisation server supplies another,
backed by its stored index, and calls the same `compute_brief` with it.

Nothing here returns source code. The query surfaces hand out derived facts --
repo names, contract names, decision titles, paths as evidence -- because any
agent with access to them could otherwise read the whole estate through them.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from ledger.adapters.vcs.base import ChangedFile, ForgeUnavailable, VcsAdapter
from ledger.adapters.vcs.bitbucket import BitbucketAdapter
from ledger.adapters.vcs.github import GitHubAdapter
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.config import Config
from ledger.core.bases import choose_base
from ledger.core.breaks import removed_contracts, removed_fields
from ledger.core.brief import (
    ContractBreak, FieldBreak, Impact, ImpactBrief, Recommendation, _names_contract, build_brief,
)
from ledger.core.contracts import ContractRef, Edge, Surface, discover, discover_in_text, http_equivalent
from ledger.core.credentials import api_host, from_git
from ledger.core.join import Change, find_siblings
from ledger.core.tickets import extract_tickets
from ledger.pipeline.collect import collect
from ledger.pipeline.decisions import load_decisions
from ledger.pipeline.scan import ScanCache
from ledger.core.branches import DEFAULT_RULES


class LedgerError(Exception):
    """A request the ledger cannot answer. `status` maps onto HTTP."""

    status = 500


class NotFound(LedgerError):
    status = 404


class Unavailable(LedgerError):
    status = 503


class BadRequest(LedgerError):
    status = 400


# ------------------------------------------------------------------ estate --

def build_adapters(cfg: Config, local_root: str | None, *, warn=None) -> list[VcsAdapter]:
    """Adapters from config. Fresh instances per operation: localgit reads a
    ref snapshot once per instance, so a long-running caller must not reuse one.

    Order of precedence: an explicit `--local-root`, then the server's own
    mirrors, then a developer's clones, then the forge APIs. Mirrors come
    before clones because they are the whole estate rather than the part of it
    somebody happened to check out.
    """
    warn = warn or (lambda _m: None)
    if local_root:
        return [LocalGitAdapter(local_root)]
    if cfg.mirror_root:
        mirrors = Path(cfg.mirror_root).expanduser()
        if not mirrors.is_dir():
            raise Unavailable(f"no mirrors at {mirrors}: run `aglium-ledger mirrors` first")
        # Checkouts first where there are any: they carry a working tree, so a
        # developer's uncommitted work stays visible, and they are the same
        # repositories. The mirrors supply everything nobody checked out.
        roots = [Path(cfg.local_root).expanduser()] if cfg.local_root else []
        return [LocalGitAdapter([*roots, mirrors])]
    if cfg.local_root:
        return [LocalGitAdapter(cfg.local_root)]
    out: list[VcsAdapter] = []
    for f in cfg.forges:
        if f.kind == "github":
            tok = os.environ.get(f.token_env or "")
            if not tok:
                warn(f"skipping github: {f.token_env} is unset")
                continue
            out.append(GitHubAdapter(f.namespace, tok))
        elif f.kind == "bitbucket":
            user, pw = os.environ.get(f.user_env or ""), os.environ.get(f.password_env or "")
            if not (user and pw):
                warn(f"skipping bitbucket: {f.user_env}/{f.password_env} unset")
                continue
            out.append(BitbucketAdapter(f.namespace, user, pw))
        else:
            raise LedgerError(f"unknown forge kind: {f.kind}")
    if not out:
        raise Unavailable("no usable forge: set credentials or pass --local-root")
    return out


def _from_git_as(f, host: str, warn) -> tuple[str, str] | None:
    """A credential from git's own store, but only as the right person.

    A developer machine holds a personal login for the same host next to the
    work one -- `jdoe` for bitbucket.org where the organisation's install
    is `jane.doe@example.com`. Taking whichever git offered would
    authenticate against the organisation's API as somebody else, which is a
    worse outcome than having no credential at all, so a mismatch is refused
    and said out loud.

    With no `api_user` configured there is nothing to check against, and the
    fallback is allowed: that is the single-account case, and the alternative
    is a feature nobody can use without naming a username they do not need.
    """
    got = from_git(host, username=f.api_user or f.git_user)
    if got is None:
        return None
    who = got[0]
    if f.api_user and who and who != f.api_user:
        warn(f"ignoring git's credential for {host}: it belongs to {who}, and this "
             f"deployment's API user is {f.api_user}")
        return None
    return got


def forge_adapters(cfg: Config, *, warn=None) -> list[VcsAdapter]:
    """Only the forges, whatever else is configured.

    Discovery is the one thing a disk cannot answer: what repositories does
    this organisation have? A mirror root can only ever report what has
    already been mirrored into it.
    """
    warn = warn or (lambda _m: None)
    out: list[VcsAdapter] = []
    for f in cfg.forges:
        # Configured environment first, always: it is the explicit answer and
        # the only one a server without a keychain will have. Falling back to
        # git's own credential store is what makes scheduled discovery work on
        # a developer machine without writing a forge token into a config
        # directory -- see `core.credentials`.
        host = api_host(f.kind)
        if f.kind == "github":
            tok = os.environ.get(f.token_env or "")
            if not tok:
                got = _from_git_as(f, host, warn)
                tok = got[1] if got else ""
            if not tok:
                warn(f"skipping github: {f.token_env} is unset and git has no usable "
                     f"credential for {host}")
                continue
            out.append(GitHubAdapter(f.namespace, tok))
        elif f.kind == "bitbucket":
            user, pw = os.environ.get(f.user_env or ""), os.environ.get(f.password_env or "")
            if not (user and pw):
                got = _from_git_as(f, host, warn)
                user, pw = got if got else (user, pw)
            if not (user and pw):
                warn(f"skipping bitbucket: {f.user_env}/{f.password_env} unset and git has no "
                     f"usable credential for {host}")
                continue
            out.append(BitbucketAdapter(f.namespace, user, pw))
        else:
            raise LedgerError(f"unknown forge kind: {f.kind}")
    return out


def forge_credentials(cfg: Config) -> dict:
    """Per-source (user, secret) for git itself, from the environment only.

    A secret of None means "let this machine's credential helper answer":
    the API credential is not always a git credential, and pretending it is
    turns a working configuration into an authentication failure on every
    repository. Nothing here is ever written anywhere.
    """
    out: dict = {}
    for f in cfg.forges:
        if f.git_credentials == "helper":
            out[f.kind] = (f.git_user, None)
            continue
        if f.kind == "github":
            tok = os.environ.get(f.token_env or "")
            if tok:
                out["github"] = (f.git_user or "x-access-token", tok)
        elif f.kind == "bitbucket":
            user, pw = os.environ.get(f.user_env or ""), os.environ.get(f.password_env or "")
            if user and pw:
                out["bitbucket"] = (f.git_user or user, pw)
    return out


def tree_reader(adapters: list[VcsAdapter]) -> VcsAdapter:
    vcs = next((a for a in adapters if a.supports_tree), None)
    if vcs is None:
        raise Unavailable("no configured source can read repositories")
    return vcs


def repos_for(cfg: Config, vcs: VcsAdapter) -> dict[str, str]:
    """Every repo the estate contains, minus the ones config excludes."""
    return {r.full_name: r.default_branch for r in vcs.list_repos()
            if r.full_name not in cfg.exclude_repos}


@dataclass
class Subject:
    repos: dict[str, str]
    base: str
    own_commits: int


#: What a client may send of its own change. A change larger than this is a
#: vendored drop or a generated-file sweep; it is briefed from what the server
#: can see instead.
WIRE_DIFF_LIMIT = 2_000_000
WIRE_TEXT_LIMIT = 1_000_000


@dataclass
class LocalChange:
    """A change as its author's machine sees it, for a server that cannot.

    An organisation's server reads mirrors. It cannot see commits that have not
    been pushed, or files that have not been committed -- and a brief is asked
    for at exactly that moment, before the push. So the client reads its own
    change and sends it: what the branch is built on, its diff, its uncommitted
    edits, and the changed files themselves, which is what tells a removed
    field from a moved one.

    This is the author's own code going to their own organisation's server,
    which mirrors the same repository. The server computes the brief from it
    and keeps none of it.
    """

    base: str
    own_commits: int
    files: list[ChangedFile] = field(default_factory=list)       # committed, against `base`
    pending: list[ChangedFile] = field(default_factory=list)     # not committed
    texts: dict[str, str] = field(default_factory=dict)          # changed files, whole

    def to_wire(self) -> dict | None:
        """The JSON a client posts, or None when the change is too large to send."""
        if sum(len(f.body or "") for f in [*self.files, *self.pending]) > WIRE_DIFF_LIMIT:
            return None
        texts, budget = {}, WIRE_TEXT_LIMIT
        for path, text in self.texts.items():
            if len(text) <= budget:
                texts[path] = text
                budget -= len(text)
        return {"base": self.base, "own_commits": self.own_commits,
                "files": [{"path": f.path, "body": f.body} for f in self.files],
                "pending": [{"path": f.path, "body": f.body} for f in self.pending],
                "texts": texts}

    @classmethod
    def from_wire(cls, doc) -> "LocalChange":
        """What a server accepts. Anything malformed is an input error."""
        def files(key: str) -> list[ChangedFile]:
            out = []
            for f in doc.get(key) or []:
                if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                    raise BadRequest(f"`{key}` must be a list of {{path, body}}")
                out.append(ChangedFile(path=f["path"], body=str(f.get("body") or "")))
            return out

        if not isinstance(doc, dict) or not isinstance(doc.get("base"), str) or not doc["base"]:
            raise BadRequest("`local` needs the branch the change is built on, as `base`")
        try:
            own = int(doc.get("own_commits") or 0)
        except (TypeError, ValueError):
            raise BadRequest("`own_commits` must be a number") from None
        texts = doc.get("texts") or {}
        if not isinstance(texts, dict):
            raise BadRequest("`texts` must be an object of path to file text")
        got = cls(doc["base"], max(own, 0), files("files"), files("pending"),
                  {str(k): str(v) for k, v in texts.items()})
        if sum(len(f.body) for f in [*got.files, *got.pending]) > WIRE_DIFF_LIMIT \
                or sum(len(t) for t in got.texts.values()) > WIRE_TEXT_LIMIT:
            raise BadRequest("the change is too large to brief from the client's copy")
        return got


def read_local_change(cfg: Config, vcs: VcsAdapter, repo: str, ref: str, *,
                      worktree: bool = True, base: str | None = None) -> LocalChange:
    """This machine's view of a change: its base, its diff, its uncommitted edits."""
    subj = resolve_subject(cfg, vcs, repo, ref, prefer=base)
    files = vcs.changed_files(repo, subj.base, ref) if subj.own_commits > 0 else []
    pending = (vcs.working_changes(repo) if worktree else None) or []
    paths = sorted({f.path for f in [*files, *pending]})
    try:
        texts = vcs.read_files(repo, ref, paths) if paths else {}
    except ForgeUnavailable:
        texts = {}
    return LocalChange(subj.base, subj.own_commits, files, pending, texts)


def resolve_subject(cfg: Config, vcs: VcsAdapter, repo: str, ref: str, *,
                    prefer: str | None = None) -> Subject:
    """Establish that (repo, ref) exists, and which branch it is built on.

    Runs BEFORE any verdict. A repo or ref that does not exist is an input
    error, not a change with no impact -- an earlier version answered one with
    PROCEED at high confidence, turning "never heard of it" into "it is fine".

    `prefer` is the branch a pull request is opened against, when the caller
    knows it. That is the base by definition, and it beats counting commits.
    """
    repos = repos_for(cfg, vcs)
    if repo not in repos:
        close = difflib.get_close_matches(repo, list(repos), n=3)
        hint = f" — did you mean: {', '.join(close)}?" if close else ""
        raise NotFound(f"unknown repo {repo!r}{hint}")
    default = repos[repo]
    if prefer:
        try:
            return Subject(repos, prefer, vcs.commits_ahead(repo, prefer, ref))
        except ForgeUnavailable:
            pass            # not a branch this repository has: work it out instead
    ahead: dict[str, int] = {}
    for cand in [default, *[b for b in cfg.integration_branches if b != default]]:
        try:
            ahead[cand] = vcs.commits_ahead(repo, cand, ref)
        except ForgeUnavailable:
            continue            # an integration branch this repo simply does not have
    if not ahead:
        raise NotFound(f"unknown ref {ref!r} in {repo}")
    base, own = choose_base(ahead, default)
    return Subject(repos, base, own)


# ------------------------------------------------------------------- index --
class Index(Protocol):
    """What a brief asks about the rest of the estate.

    Two implementations: `LiveIndex` below, which reads the estate for one
    call and answers "nothing" to everything only a stored index can know,
    and the organisation server's view over its database.
    """

    source: str
    live_repos: list[str]
    unreadable: list[str]

    def decisions(self): ...
    def depended_contracts(self, *, exclude: str | None = None) -> list: ...
    def parties(self, contract, *, exclude: str | None = None) -> list[dict]: ...
    def documented_in(self, contracts) -> dict: ...
    def synced_at(self) -> str | None: ...
    def integration_here(self, repo: str, contracts) -> dict: ...
    def moving_on(self, contracts) -> dict: ...
    def inflight(self, repo: str, ref: str, tickets): ...
    def siblings_by_tickets(self, tickets, *, exclude_repo=None): ...
    def siblings_near(self, *, author, at, ref="", exclude_repo=None): ...
    def siblings_by_signal(self, signals, *, exclude_repo=None): ...
    def behaviours(self, commits) -> dict: ...
    def topics_bound_in(self, repo: str) -> dict: ...
    def channel_peers(self, topics) -> dict: ...
    def field_deps(self, contracts, *, exclude: str | None = None) -> dict: ...
    def fields_of(self, contracts, *, exclude: str | None = None) -> dict: ...
    def owners_for(self, contracts, *, exclude: str | None = None) -> dict: ...


class LiveIndex:
    """No database: read the estate for this call. Same interface as the store.

    What a stored index remembers, this reads from the code each time, and
    only literally: which way each repository faces a contract (from its call
    sites), which payload names another repository reads (from its code), and
    which other changes in flight are about the same thing (from their diffs).
    """

    source = "live"

    #: Other changes in flight read per brief, newest first, to link work that
    #: shares no ticket key. Each costs one diff.
    LINK_CAP = 40
    #: "Around the same time", for two changes by the same author.
    NEAR = timedelta(hours=2)
    #: A shared signal carried by more of the other changes than this links nothing.
    RARE = 3

    def __init__(self, vcs: VcsAdapter, repos: dict[str, str], cache: ScanCache | None = None):
        self.vcs, self.repos = vcs, repos
        self.live_repos = sorted(repos)
        self.unreadable: list[str] = []
        #: Scans remembered by commit, so a repository that has not moved is
        #: not read again. None reads everything, every time.
        self.cache = cache
        self._uses_memo: dict = {}
        self._population: list[Change] = []
        self._cfg: Config | None = None
        self._diffs: dict = {}

    # -- what each repository does with a contract ------------------------------

    def _uses(self, repo: str, ref: str) -> dict:
        """`{ContractRef: (edge, [paths])}` for one repository. Raises ForgeUnavailable."""
        key = (repo, ref)
        if key not in self._uses_memo:
            if self.cache is not None:
                self._uses_memo[key] = self.cache.uses(self.vcs, repo, ref)
            else:
                from ledger.pipeline.scan import read_repo
                self._uses_memo[key] = read_repo(self.vcs, repo, ref)[0]
        return self._uses_memo[key]

    def _use(self, repo: str, ref: str, contract) -> tuple | None:
        try:
            uses = self._uses(repo, ref)
        except ForgeUnavailable:
            if repo not in self.unreadable:
                self.unreadable.append(repo)
            return None
        hit = uses.get(contract)
        if hit is None and contract.surface == Surface.HTTP:
            hit = next((v for c, v in sorted(uses.items(), key=lambda kv: kv[0].name)
                        if c.surface == Surface.HTTP and http_equivalent(c.name, contract.name)), None)
        return hit

    def decisions(self):
        if self.cache is None:
            return load_decisions(self.vcs, self.repos)
        out: list = []
        unreadable: list[str] = []
        for repo, ref in self.repos.items():
            try:
                out.extend(self.cache.get(self.vcs, repo, ref)[1])
            except ForgeUnavailable:
                unreadable.append(repo)
        return out, unreadable

    def depended_contracts(self, *, exclude: str | None = None) -> list:
        return []

    def parties(self, contract, *, exclude: str | None = None) -> list[dict]:
        """Who else names `contract`, and which way each faces it, from their call sites.

        Topics and routes. A table is left out: every line naming one reads as
        a write, so the code does not say who owns it and who only reads.
        """
        if contract.surface == Surface.TABLE:
            return []
        out = []
        for other, ref in self.repos.items():
            if other == exclude:
                continue
            hit = self._use(other, ref, contract)
            if hit is None:
                continue
            edge, paths = hit
            verb = {("http", "publishes"): "serves it", ("http", "consumes"): "calls it",
                    ("topic", "publishes"): "publishes it",
                    ("topic", "consumes"): "consumes it"}.get((contract.surface.value, edge),
                                                              "names it")
            out.append({"repo": other, "uses": edge,
                        "how": f"{verb} in `{paths[0]}`" if paths else verb,
                        "evidence": paths[:2], "branches": [], "about": None})
        return out

    def field_readers(self, contracts, names, *, exclude: str | None = None) -> dict:
        """`{contract: {name: [reader]}}`: other repositories whose code reads one
        of `names` from a contract's payload.

        The live half of the field layer, asked only about the names a change
        removes. A reader is a repository that consumes the topic or calls the
        route, in a file that names it, using the name as a field -- a quoted
        key or an attribute, the same shapes `core.breaks` accepts -- rather
        than merely containing the word. Literal: a field read through a
        generated client or another module is not seen.
        """
        from ledger.core.breaks import _COMMENT, _structural
        out: dict = {}
        names = sorted({n for n in names if n and len(n) >= 3})
        if not names:
            return out
        for c in contracts:
            if c.surface == Surface.TABLE:
                continue
            for other, ref in self.repos.items():
                if other == exclude:
                    continue
                hit = self._use(other, ref, c)
                if hit is None or hit[0] == Edge.PUBLISHES.value:
                    continue                    # another writer, not a reader
                try:
                    texts = self.vcs.read_files(other, ref, hit[1])
                except ForgeUnavailable:
                    continue
                for name in names:
                    word = re.compile(rf"(?<!\w){re.escape(name)}\b")
                    where = next((p for p, text in sorted(texts.items())
                                  for line in text.splitlines()
                                  if word.search(line) and not _COMMENT.match(line)
                                  and _structural(name, line)), None)
                    if where:
                        out.setdefault(c, {}).setdefault(name, []).append(
                            {"repo": other, "direction": "reads", "evidence": where,
                             "role": f"reads `{name}` in {where}"})
        return out

    def separate_tables(self, owners: dict, repo: str, ref: str, files) -> dict:
        """Drop repositories that only share a table's NAME with this one.

        Two repositories that each create the table in their own migrations
        have two tables. See `core.contracts.own_table`, measured on a real
        estate: `audit_log` had fourteen definers.
        """
        from ledger.core.contracts import own_table
        out = {}
        for c, others in owners.items():
            if c.surface != Surface.TABLE:
                out[c] = others
                continue
            hit = self._use(repo, ref, c)
            mine = (hit[1] if hit else []) + [f.path for f in files if c.name in (f.body or "")]
            kept = []
            for other in others:
                theirs = self._use(other, self.repos.get(other, ""), c)
                if not own_table({repo: mine, other: theirs[1] if theirs else []}):
                    kept.append(other)
            if kept:
                out[c] = kept
        return out

    def documented_in(self, contracts) -> dict:
        """No database, no documentation index."""
        return {}

    def synced_at(self) -> str | None:
        """Read live for this call: there is no stored state to be stale."""
        return None

    def integration_here(self, repo: str, contracts) -> dict:
        """No database: integration-branch edges are not read live."""
        return {}

    def moving_on(self, contracts) -> dict:
        """No database: branch-level edges are not read live."""
        return {}

    def inflight(self, repo: str, ref: str, tickets):
        """No database: the caller must enumerate branches itself."""
        return None

    # -- other work in flight, linked without a ticket key ------------------------

    def use_changes(self, changes, cfg: Config | None = None) -> None:
        """The work in flight this brief may link to, as the caller enumerated it."""
        rules = cfg.branches if cfg is not None else DEFAULT_RULES
        self._population = [c for c in changes
                            if c.kind != "integration" and rules.allows(c.ref)]
        self._cfg = cfg

    def _others(self, exclude_repo: str | None) -> list[Change]:
        got = [c for c in self._population if c.repo != exclude_repo]
        got.sort(key=lambda c: c.updated_at.timestamp() if c.updated_at else 0, reverse=True)
        return got[:self.LINK_CAP]

    def diff_range(self, repo: str, ref: str, kind: str, sha: str | None) -> tuple[str, str] | None:
        """What an in-flight change is diffed against, as `(base, head)`."""
        cfg = self._cfg or Config(org="", project_keys=())
        return _impact_base(cfg, self.vcs, self.repos, repo, ref, kind, sha)

    def _files(self, c: Change) -> list:
        from ledger.pipeline.signals import DIFF_LIMIT
        key = (c.repo, c.ref, c.sha)
        if key not in self._diffs:
            files: list = []
            if self.vcs.supports_diffs:
                try:
                    pair = self.diff_range(c.repo, c.ref, c.kind, c.sha)
                    files = self.vcs.changed_files(c.repo, *pair) if pair else []
                except ForgeUnavailable:
                    files = []
            if sum(len(f.body or "") for f in files) > DIFF_LIMIT:
                files = []
            self._diffs[key] = files
        return self._diffs[key]

    def siblings_by_tickets(self, tickets, *, exclude_repo=None):
        """Changes in flight elsewhere whose branch carries a ticket this diff names."""
        wanted = {t.upper() for t in tickets}
        if not wanted:
            return []
        from ledger.core.join import Sibling
        return [Sibling(c, tuple(sorted(wanted & {t.upper() for t in c.tickets})))
                for c in self._population
                if c.repo != exclude_repo and wanted & {t.upper() for t in c.tickets}]

    def siblings_near(self, *, author, at, ref="", exclude_repo=None):
        """The same author's work elsewhere, started within `NEAR` of this one."""
        if not author or at is None:
            return []
        from ledger.core.join import Sibling
        return [Sibling(c, tuple(c.tickets)) for c in self._others(exclude_repo)
                if (c.author or "").lower() == author.lower() and c.updated_at is not None
                and abs(c.updated_at.timestamp() - at.timestamp()) <= self.NEAR.total_seconds()]

    def siblings_by_signal(self, signals, *, exclude_repo=None):
        """Changes elsewhere whose diffs share a RARE topic, table or migration with this one.

        Rare among the other changes in flight: two coupled repositories share
        their whole event vocabulary, and a name every change touches links
        nothing (see `pipeline.signals`).
        """
        if not signals:
            return []
        from collections import Counter

        from ledger.core.join import Sibling
        from ledger.core.overlap import WEIGHT
        from ledger.pipeline.signals import signals_of
        others = self._others(None)
        theirs = {id(c): signals_of(self._files(c)) for c in others}
        freq = Counter(s for sigs in theirs.values() for s in sigs)
        out = []
        for c in others:
            if c.repo == exclude_repo:
                continue
            shared = {s for s in theirs[id(c)] & set(signals) if freq[s] <= self.RARE}
            if shared:
                out.append((Sibling(c, tuple(c.tickets)), sum(WEIGHT[s.kind] for s in shared),
                            sorted(f"{s.kind.value} `{s.value}`" for s in shared)))
        out.sort(key=lambda t: -t[1])
        return out

    def siblings_by_contract(self, contracts, *, exclude_repo=None):
        """Changes elsewhere whose diffs name a topic or route this change touches.

        Tables are left out, for the reason `parties` gives.
        """
        from ledger.core.effect import _same, delta_of
        from ledger.core.join import Sibling
        wanted = [c for c in contracts if c.surface != Surface.TABLE]
        if not wanted:
            return []
        out = []
        for c in self._others(exclude_repo):
            d = delta_of(c.repo, self._files(c))
            shared = sorted({x.name for x, edges in d.contracts.items()
                             if edges - {Edge.MENTIONS} and any(_same(x, y) for y in wanted)})
            if shared:
                out.append((Sibling(c, tuple(c.tickets)), shared))
        return out

    def topics_bound_in(self, repo: str) -> dict:
        """Topics `repo` reaches through a variable, from its own config and code.

        `{ident: {"raws": set, "values": set}}`. Read from local git only: on a
        forge API it would mean reading every file.
        """
        ref = self.repos.get(repo)
        if ref is None:
            return {}
        key = ("bound", repo, ref)
        if key not in self._uses_memo:
            got: dict = {}
            try:
                if self.cache is not None:
                    got = self.cache.bound(self.vcs, repo, ref)
                else:
                    from ledger.pipeline.scan import wiring_of
                    got = wiring_of(self.vcs, repo, ref)[0]
            except ForgeUnavailable:
                got = {}
            self._uses_memo[key] = {i: {"raws": set(v["raws"]), "values": set(v["values"])}
                                    for i, v in got.items()}
        return self._uses_memo[key]

    def behaviours(self, commits) -> dict:
        """No database, so no stored model summaries. Nothing is asked live:
        a brief is not the moment to wait on a model."""
        return {}

    def channel_peers(self, topics) -> dict:
        """No database: declarations are not read live."""
        return {}

    def field_deps(self, contracts, *, exclude: str | None = None) -> dict:
        """No stored field layer. `field_readers` answers the narrower live
        question: who reads the names this change removes."""
        return {}

    def fields_of(self, contracts, *, exclude: str | None = None) -> dict:
        """No database, no field layer."""
        return {}

    def owners_for(self, contracts, *, exclude: str | None = None) -> dict:
        owners: dict = {}
        if not contracts:
            return owners
        wanted = set(contracts)
        for other, other_ref in self.repos.items():
            if other == exclude:
                continue
            try:
                found = set(self._uses(other, other_ref))
            except ForgeUnavailable:
                if other not in self.unreadable:
                    self.unreadable.append(other)
                continue
            for c in found & wanted:
                owners.setdefault(c, []).append(other)
        return {c: sorted(rs) for c, rs in owners.items()}

# ------------------------------------------------------------------- brief --

def compute_brief(cfg: Config, adapters: list[VcsAdapter], repo: str, ref: str,
                  open_index, *, collected=None, worktree: bool = False,
                  allow_missing_ref: bool = False, subject: Subject | None = None,
                  local: LocalChange | None = None, base: str | None = None) -> ImpactBrief:
    """The impact brief. `open_index(repos)` returns a LiveIndex or StoredView.

    `collected` lets a batch caller (an evaluation over every open change)
    enumerate in-flight work once instead of once per brief.

    `allow_missing_ref` answers about a branch that does not exist yet -- the
    moment it is created is the earliest anything can be said, and what can be
    said is what else is in flight under the ticket in its name.

    `worktree` includes uncommitted changes in a local clone. That is where an
    agent session lives: one real session changed nine files and committed
    none of them, and every answer about it said "no commits beyond master, so
    it touches no contracts". Only the caller knows the working tree belongs
    to this ref -- a clone has one, and it may be on another branch entirely.

    `subject` says what the change is built on instead of working it out. A
    replay needs that: a change merged months ago has no commits beyond the
    default branch today, and its base is the commit it was merged onto.

    `local` is the change as its author's machine sees it, sent by a client to
    a server that only has mirrors: unpushed commits and uncommitted files are
    not in a mirror. Given, it replaces every read of the change itself; the
    rest of the estate is still read here.
    """
    vcs = tree_reader(adapters)
    missing_ref = False
    if local is not None and subject is None:
        repos = repos_for(cfg, vcs)
        if repo not in repos:
            close = difflib.get_close_matches(repo, list(repos), n=3)
            raise NotFound(f"unknown repo {repo!r}"
                           + (f" — did you mean: {', '.join(close)}?" if close else ""))
        subject = Subject(repos, local.base, local.own_commits)
    try:
        subj = subject or resolve_subject(cfg, vcs, repo, ref, prefer=base)
    except NotFound:
        if not allow_missing_ref:
            raise
        repos = repos_for(cfg, vcs)
        if repo not in repos:
            raise
        # Not "zero commits beyond the base" -- unknown. A branch that does
        # not exist yet has not integrated its work anywhere, and saying so
        # was the one wrong line in an otherwise useful answer.
        subj = Subject(repos, repos[repo], 0)
        missing_ref = True
    index = open_index(subj.repos)

    # In-flight work: from the stored index where there is one. Re-enumerating
    # every branch in the organisation on every brief cost seven seconds on a
    # 171-repository estate to reproduce what the last sync already knew. The
    # answer carries that sync's age instead.
    result = None if collected is not None else index.inflight(
        repo, ref, tuple(extract_tickets(ref, cfg.project_keys)))
    if result is None:
        got = collected or collect(adapters, cfg.project_keys, liveness_days=cfg.liveness_days,
                                   integration_branches=cfg.integration_branches,
                                   exclude_repos=cfg.exclude_repos,
                                   branches=cfg.branches)
        subject = next((c for c in got.changes if c.repo == repo and c.ref == ref), None)
        if subject is None:
            # The ref exists (resolved above) but is outside the liveness window
            # or carries no ticket key. Still a real change; it joins on nothing.
            subject = Change(repo=repo, ref=ref,
                             tickets=tuple(extract_tickets(ref, cfg.project_keys)))
        result = find_siblings(subject, got.changes,
                               partial=got.partial, missing_sources=got.missing_sources)
        if hasattr(index, "use_changes"):
            index.use_changes(got.changes, cfg)

    decisions, unreadable = index.decisions()

    # Diff against the branch the change is actually built on. A change with
    # no commits beyond its base is already integrated and touches nothing of
    # its own -- so there is no diff to read, and nothing to look up.
    changed: list[str] = []
    touched: list = []
    owners: dict = {}
    breaks: list[FieldBreak] = []
    gone_contracts: list[ContractBreak] = []
    parties: dict = {}
    here: dict = {}
    diff_tickets: list[str] = []
    subject_signals: set = set()
    files: list = []
    edges: list = []
    bound_edges: dict = {}
    if local is not None:
        pending = list(local.pending)
    else:
        pending = (vcs.working_changes(repo) if worktree else None) or []
    if (vcs.supports_diffs or local is not None) and (subj.own_commits > 0 or pending):
        try:
            if local is not None:
                files = list(local.files)
            else:
                files = vcs.changed_files(repo, subj.base, ref) if subj.own_commits > 0 else []
        except ForgeUnavailable:
            files = []
        # Uncommitted work last, so a file changed both ways keeps both hunks.
        files = files + [f for f in pending]
        changed = [f.path for f in files]
        # The ticket a branch name does not carry. From the feedback: "the
        # ticket key appears in the diff body (the code docstring and contract
        # doc both say XY-525) even though it's missing from the branch name
        # and commit message", and the brief said `proceed` while the other
        # half of the change sat in another repository.
        in_diff = set(extract_tickets(" ".join(f.body or "" for f in files), cfg.project_keys))
        diff_tickets = sorted(in_diff - set(extract_tickets(ref, cfg.project_keys)))
        # What this change touches, from the diff already in hand. Computed
        # rather than looked up so a branch created since the last sync -- the
        # moment somebody asks about it -- still links.
        from ledger.pipeline.signals import signals_of
        subject_signals = signals_of(files)
        edges = []
        for f in files:
            edges.extend(discover_in_text(repo, f.path, f.body))
        touched_set = {e.contract for e in edges}
        # A change often touches a topic through its constant, not the literal:
        # `producer.send(INVOICE_EVENTS_TOPIC, ...)`. Bindings stored for this
        # repo say which topic that constant is.
        bound = index.topics_bound_in(repo)
        if bound:
            from ledger.core.bindings import usages_in
            idents = {i: v["raws"] for i, v in bound.items()}
            for f in files:
                for u in usages_in(f.path, f.body, idents):
                    # Every topic the identifier is bound to, not the
                    # alphabetically first one. An identifier here is
                    # routinely bound twice -- 11 of 21 in one repository --
                    # to the literal default in code (`intake.drafts.v1`) and
                    # to the name the deployment actually uses
                    # (`acme-orchestrator-service.intake-drafts`). Only the
                    # second has declared parties, and sorting put the first
                    # one in front, so the brief named a topic nobody
                    # produced or consumed. Worse where the deployed value
                    # belongs to another app entirely
                    # (`apex_document_result_topic` ->
                    # `acme-doc-validation.document-results`):
                    # that is the cross-repo party, discarded by a sort.
                    for value in sorted(bound[u.ident]["values"]):
                        touched_set.add(ContractRef(Surface.TOPIC, value))
                        bound_edges.setdefault(ContractRef(Surface.TOPIC, value),
                                               set()).add(Edge(u.edge))
        touched = sorted(touched_set, key=lambda c: (c.surface.value, c.name))
        owners = index.owners_for(touched, exclude=repo)
        # Work in flight *inside this repository*. The original rule excluded it
        # because "same-repo siblings are already visible to the reviewer --
        # they are in the same PR list". That is false twice over for an agent:
        # it has no PR list, and a change merged to an integration branch is in
        # nobody's PR list because it has already merged. A branch cut from the
        # default branch cannot see it at all.
        # Not the branch this change is built on: work already in the base is
        # already in the change. Only an integration branch it is NOT built on
        # holds work that will land alongside it.
        here = {c: [r for r in refs if r != subj.base]
                for c, refs in index.integration_here(repo, touched).items()}
        here = {c: refs for c, refs in here.items() if refs}
        # Topics: who really produces and consumes, not who merely names them.
        #
        # Plus the next repository along the pipe. A deployment that names
        # topics `{app}.{value}` gives one channel several topic names, and
        # the miss this answers is exactly that: "brief only listed
        # `intake.drafts.v1` with no parties, while leads publishes to
        # `acme-leads.intake-drafts`... Because of this miss, the review
        # skipped the leads side, where real issues were." Both halves were
        # on one channel and no two of their topic names matched.
        live = index.source == "live"
        if live and hasattr(index, "separate_tables"):
            owners = index.separate_tables(owners, repo, subj.base, files)
        peers = index.channel_peers([c.name for c in touched if c.surface == Surface.TOPIC])
        for c in touched:
            # Live, routes too: who serves and who calls is read from the code.
            if c.surface != Surface.TOPIC and not (live and c.surface == Surface.HTTP):
                continue
            got = index.parties(c, exclude=repo)
            for p in peers.get(c.name, ()):
                if p["repo"] == repo or any(g["repo"] == p["repo"] for g in got):
                    continue
                got.append({"repo": p["repo"], "uses": p["edge"],
                            "how": f"same channel `{p['channel']}`, declared as `{p['topic']}`",
                            "evidence": [p["evidence"]], "branches": [], "about": None})
            if got:
                parties[c] = got
                directed = [p["repo"] for p in got if p["uses"] in ("publishes", "consumes")]
                # A live read names every repository that mentions the
                # contract; the direction adds to that, it does not narrow it.
                owners[c] = sorted(set(owners.get(c, [])) | set(directed)) if live else directed

        # Does this diff delete a field another repo depends on? The one
        # finding here that is evidence rather than inference.
        diffs = {f.path: f.body for f in files}
        try:
            texts = vcs.read_files(repo, ref, list(diffs))
        except ForgeUnavailable:
            texts = {}
        if local is not None:
            # The author's copy is the newer one: the branch may not be pushed.
            texts = {**texts, **local.texts}
        # Candidates come from the FILES, not only the hunks. A diff that
        # deletes `matched_recipient_id: UUID | None = None` never names the
        # topic that carries it -- the file does, and that is what matters.
        candidates = set(touched) | {
            c for c in index.depended_contracts(exclude=repo)
            if any(c.name in body for body in texts.values())
        }
        # The contract itself, not just its fields: deleting the route is the
        # larger sibling of deleting one of its fields.
        def bound_after(raw: str) -> set[str]:
            from ledger.core.bindings import bindings_in
            try:
                paths = vcs.grep_paths(repo, ref, [raw])
                found = vcs.read_files(repo, ref, paths)
            except ForgeUnavailable:
                return set()
            return {b.value for p, text in found.items() for b in bindings_in(p, text) if b.raw == raw}

        from ledger.core.breaks import moved_binding
        for gone_c in removed_contracts(diffs, owners, texts):
            if moved_binding(gone_c.evidence, gone_c.contract.name, bound_after):
                continue            # the definition moved; the topic is still bound
            gone_contracts.append(ContractBreak(
                surface=gone_c.contract.surface.value, name=gone_c.contract.name,
                also_touched_by=sorted(owners.get(gone_c.contract, [])),
                evidence=gone_c.evidence, path=gone_c.path,
            ))

        deps = index.field_deps(sorted(candidates, key=lambda c: (c.surface.value, c.name)),
                                exclude=repo)
        about = dict(texts)
        if not deps and hasattr(index, "field_readers"):
            # No stored field layer: ask who reads the names this diff removes,
            # from the contracts the changed FILES name -- a deleted payload
            # key rarely sits on the line that names its topic.
            from ledger.core.effect import delta_of
            named = set(candidates)
            for path, text in texts.items():
                named |= {e.contract for e in discover_in_text(repo, path, text)}
                if bound:           # a topic the file reaches through a constant
                    from ledger.core.bindings import usages_in
                    reached = {v for u in usages_in(path, text, {i: v["raws"] for i, v in bound.items()})
                               for v in bound[u.ident]["values"]}
                    named |= {ContractRef(Surface.TOPIC, v) for v in reached}
                    if reached:
                        # So `removed_fields` sees the file as about the topic
                        # it never spells. Used for that check only.
                        about[path] = text + "\n" + "\n".join(sorted(reached))
            gone = delta_of(repo, files).keys_removed
            deps = index.field_readers(sorted(named, key=lambda c: (c.surface.value, c.name)),
                                       gone, exclude=repo)
        if deps:
            wanted = {c: list(fields) for c, fields in deps.items()}
            for gone in removed_fields(diffs, wanted, file_text=about):
                breaks.append(FieldBreak(
                    surface=gone.contract.surface.value, name=gone.contract.name,
                    field_path=gone.field_path, readers=deps[gone.contract][gone.field_path],
                    evidence=gone.evidence, path=gone.path,
                ))

    b = build_brief(result, decisions, changed_paths=changed,
                    same_repo_integration=here,
                    contracts=touched, contract_owners=owners, field_breaks=breaks,
                    contract_breaks=gone_contracts, contract_parties=parties,
                    contract_docs=index.documented_in(touched),
                    contract_fields={c: v for c, v in index.fields_of(touched, exclude=repo).items()
                                     if owners.get(c)},
                    integration_branches=tuple(cfg.integration_branches),
                    resolved=True, base=subj.base,
                    own_commits=None if missing_ref else subj.own_commits,
                    uncommitted=[f.path for f in pending])
    _link_without_a_ticket_key(b, index, repo, ref, diff_tickets, result, subject_signals,
                               touched=touched)
    yours: dict = {}
    for e in edges:
        yours.setdefault(e.contract, set()).add(e.edge)
    for c, es in bound_edges.items():   # reached through a bound constant
        yours.setdefault(c, set()).update(es)
    describe_impacts(b.impacts, cfg, vcs, subj.repos, index, yours=yours,
                     your_files=files, contracts=b.contracts)
    _escalate_opposite_moves(b)
    # A topic name removed from code may only be a default: if deployment config
    # binds the same identifier to another name, the running topic may not
    # change where that override applies. Say so rather than overstate.
    for cb in b.contract_breaks:
        if cb.surface != "topic":
            continue
        for ident, v in index.topics_bound_in(repo).items():
            others = sorted(v["values"] - {cb.name})
            if cb.name in v["values"] and others:
                b.reasons.append(
                    f"note: `{sorted(v['raws'])[0]}` is also bound to `{others[0]}` in deployment "
                    f"config; where that override applies, the topic in use does not change — "
                    f"this change moves the default")
                break
    b.index_source, b.live_repos = index.source, index.live_repos
    b.synced_at = index.synced_at()
    b.unreadable_repos = sorted(set(unreadable) | set(index.unreadable))
    b.changed_paths = changed
    b.diff_names = diff_names(files)
    from ledger.core.rank import rank_brief
    b.ranked = rank_brief(b.to_dict())
    return b


def diff_names(files) -> list[str]:
    """Identifiers a diff's changed lines name, most frequent first. Names, not code."""
    import collections
    seen: collections.Counter = collections.Counter()
    for f in files:
        for line in (f.body or "").splitlines():
            seen.update(t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", line)
                        if "_" in t or any(c.isupper() for c in t[1:]))
    return [t for t, _ in seen.most_common(200)]


#: In-flight items described per answer. Each costs a diff of another
#: repository; renderers show six, and a seventh nobody reads is not worth one.
DESCRIBED = 6

#: (repo, commit, base) -> Delta. A commit's diff does not change, so this is
#: safe to keep for the life of the process; the bound stops a long-running
#: server holding every branch it has ever been asked about.
_DELTAS: dict[tuple, object] = {}
_DELTAS_MAX = 512


def _impact_base(cfg: Config, vcs: VcsAdapter, repos: dict[str, str], repo: str, ref: str,
                 kind: str, sha: str | None) -> tuple[str, str] | None:
    """What to diff an in-flight change against, as `(base, head)`.

    A merged commit is read against its first parent: for a merge commit that
    is exactly what the merge brought in. A branch is read against whichever
    of the default and integration branches it is built on, the same choice
    the subject gets -- a branch cut from `stage` and read against `master`
    would be credited with everything unreleased.
    """
    if kind == "integration":
        return (f"{sha}^", sha) if sha else None
    default = repos.get(repo)
    if default is None:
        return None
    ahead: dict[str, int] = {}
    for cand in [default, *[x for x in cfg.integration_branches if x != default]]:
        try:
            ahead[cand] = vcs.commits_ahead(repo, cand, ref)
        except ForgeUnavailable:
            continue
    if not ahead:
        return None
    return choose_base(ahead, default)[0], ref


def impact_delta(cfg: Config, vcs: VcsAdapter, repos: dict[str, str], index, repo: str,
                 ref: str, kind: str, sha: str | None):
    """The `Delta` of one in-flight change and the base it was read against,
    or `(None, None)` when its diff cannot be read. Cached by commit."""
    from ledger.core.effect import delta_of
    from ledger.pipeline.signals import DIFF_LIMIT

    # A release train's diff is everything unreleased in its repository. That
    # is not what one change does, and it is megabytes.
    if not vcs.supports_diffs or (kind != "integration" and not cfg.branches.allows(ref)):
        return None, None
    try:
        pair = (index.diff_range(repo, ref, kind, sha) if hasattr(index, "diff_range")
                else _impact_base(cfg, vcs, repos, repo, ref, kind, sha))
        if pair is None:
            return None, None
        base, head = pair
        shown = None if kind == "integration" else base
        key = (repo, sha, base) if sha else None
        if key in _DELTAS:
            return _DELTAS[key], shown
        files = vcs.changed_files(repo, base, head)
    except ForgeUnavailable:
        return None, None
    if sum(len(f.body or "") for f in files) > DIFF_LIMIT:
        return None, None
    # Topics reached through a constant, the same way the subject's are: a
    # producer that calls `send(TOPIC_INTAKE, ...)` never spells the topic.
    extra: dict = {}
    bound = index.topics_bound_in(repo) if index is not None else {}
    if bound:
        from ledger.core.bindings import usages_in
        idents = {i: v["raws"] for i, v in bound.items()}
        for f in files:
            for u in usages_in(f.path, f.body, idents):
                for value in sorted(bound[u.ident]["values"]):
                    extra.setdefault(ContractRef(Surface.TOPIC, value), set()).add(
                        Edge(u.edge))
    delta = delta_of(repo, files, extra=extra)
    if key is not None:
        if len(_DELTAS) >= _DELTAS_MAX:
            _DELTAS.clear()
        _DELTAS[key] = delta
    return delta, shown


def describe_impacts(impacts: list[Impact], cfg: Config, vcs: VcsAdapter, repos: dict[str, str],
                     index, *, yours: dict, your_files: list, contracts: list) -> None:
    """Fill `effect`, `relation`, `evidence` and `ask` on each in-flight item.

    Until this existed an item was a repository, a branch and an author. One
    review took such a pointer, read the other branch's commit titles, and
    reported the opposite of what the branch does; its author had to correct
    it. The repository the reader needs is usually not on their disk, so what
    the other change does has to arrive already derived.

    Branches before merged commits, the order renderers show them in, and no
    more than `DESCRIBED`: every one is another repository's diff.
    """
    from ledger.core.effect import ask_line, delta_of, effect_line, evidence_line, relation_line

    from ledger.core.effect import Delta, opposite_moves
    mine = delta_of("", your_files) if your_files else Delta()
    your_keys = mine.keys_added | mine.keys_removed
    chosen = sorted(impacts, key=lambda i: bool(i.where))[:DESCRIBED]
    # What a model made of each one at sync time, where there is a server
    # that asked. Read, never requested here.
    summaries = index.behaviours([(i.repo, i.sha) for i in chosen if i.sha]) \
        if index is not None else {}
    for i in chosen:
        i.behaviour = summaries.get((i.repo, i.sha))
        delta, base = impact_delta(cfg, vcs, repos, index, i.repo, i.ref, i.kind, i.sha)
        i.evidence = evidence_line(sha=i.sha, where=i.where, base=base,
                                   summarised=delta is not None)
        if delta is None:
            i.ask = ask_line(i.ref, i.author, None, [])
            continue
        i.effect = effect_line(delta)
        # What the index knows of the other repository's part in a contract
        # this change touches, for when the two diffs share no name.
        role, known = None, []
        for ci in contracts:
            for p in ci.parties:
                if p["repo"] == i.repo and p["uses"] in ("publishes", "consumes") and role is None:
                    role = f"{p['uses']} {ci.surface} `{ci.name}`"
            known += [f for f in ci.fields if f["repo"] == i.repo]
        i.relation, shared, fields = relation_line(delta, yours, your_keys=your_keys,
                                                   known_fields=known, repo_role=role)
        i.over = f"{shared.contract.surface.value} `{shared.contract.name}`" if shared else None
        i.conflict = opposite_moves(delta, mine)
        i.ask = ask_line(i.ref, i.author, shared, fields)


#: A verdict this may overturn. `review_with` and anything stronger already
#: tells the reader to go and look; only the two that say "nothing found" are
#: made wrong by finding something.
_QUIET = ("proceed", "insufficient_data")


def _link_without_a_ticket_key(b: ImpactBrief, index, repo: str, ref: str,
                               diff_tickets: list[str], result,
                               subject_signals: set | None = None, *,
                               touched=()) -> None:
    """Work in another repository that the branch name could not point at.

    The failure this exists for, in the reviewer's words: "brief returned
    proceed / high confidence / no cross-repo work shares this ticket because
    the branch name and commit message carry no ticket key... Linking cannot
    depend on ticket ids." The producer half of that change was in
    `acme-leads`, pushed seven minutes later by the same author, and the
    review that trusted the brief skipped it -- where the real issues were.

    Two signals, both added to the brief with the reason they were found, and
    both weaker than a ticket key in the branch name. The ticket-in-the-diff
    one is nearly as strong as the branch-name one -- somebody wrote the key
    in the code. Proximity by author is a question, not a finding, so it says
    so and never carries a verdict of its own.
    """
    known = {(i.repo, i.ref) for i in b.impacts}
    found: list[tuple[str, str]] = []

    for sib in index.siblings_by_tickets(diff_tickets, exclude_repo=repo):
        c = sib.change
        if (c.repo, c.ref) in known:
            continue
        known.add((c.repo, c.ref))
        found.append((c, f"shares {', '.join(sib.shared_tickets)}, which this diff names and the "
                         f"branch name does not"))

    # The subject has to be somebody's own branch too. An integration branch
    # being briefed is a release, not a change, and its author is whoever last
    # merged into it.
    subject = getattr(result, "subject", None) if result is not None else None
    own_branch = getattr(subject, "kind", None) in ("branch", "pr")
    # Another change whose diff names a topic or route this one touches: the
    # strongest link short of a ticket, because it says over what.
    by_contract = getattr(index, "siblings_by_contract", None)
    for sib, shared in (by_contract(touched, exclude_repo=repo) if by_contract else ()):
        c = sib.change
        if (c.repo, c.ref) in known:
            continue
        known.add((c.repo, c.ref))
        found.append((c, f"its diff also names {', '.join(f'`{n}`' for n in shared[:3])}, which "
                         "this change touches; no ticket links them"))

    for sib in (index.siblings_near(author=getattr(subject, "author", None),
                                    at=getattr(subject, "updated_at", None),
                                    ref=ref, exclude_repo=repo) if own_branch else ()):
        c = sib.change
        if (c.repo, c.ref) in known:
            continue
        known.add((c.repo, c.ref))
        found.append((c, "same author, pushed around the same time; no ticket links them, so "
                         "check whether it is the other half of this change"))

    # What this change touches that another change rarely touches. The link of
    # last resort, and the only one left when nobody typed a ticket key: a
    # quarter of live branches on a real estate carry none anywhere.
    for sib, score, shared in index.siblings_by_signal(subject_signals or set(),
                                                       exclude_repo=repo):
        c = sib.change
        if (c.repo, c.ref) in known:
            continue
        known.add((c.repo, c.ref))
        found.append((c, f"both change {', '.join(shared)}, which little else touches "
                         f"(score {score}); no ticket links them"))

    if not found:
        return
    # Read live, "same author, around the same time" alone is a question, not
    # a finding: it is listed, and the verdict does not move on it. On a
    # labelled estate it was the only rule that linked unrelated work -- two
    # documentation fixes half an hour apart. A stored index weighs it with
    # everything else it knows about both changes.
    near_only = index.source == "live" and all(why.startswith("same author") for _, why in found)
    for c, why in found[:4]:
        # Into `impacts` only. Renderers already lift in-flight work into the
        # block of things the repository cannot show, so adding it to
        # `not_local` as well printed every one of these twice.
        b.impacts.append(Impact(
            repo=c.repo, ref=c.ref, kind=c.kind, author=c.author,
            shared_tickets=list(c.tickets), where=c.integration_ref, sha=c.sha, how=why))
    # The verdict's own sentence is now false. Left in place it read "no
    # cross-repo work shares this ticket" directly above the cross-repo work.
    if near_only:
        b.reasons.append(f"note: {len(found)} change(s) by the same author in other repo(s), "
                         "started around the same time, are listed below as a question; nothing "
                         "links them to this one")
        return
    b.reasons = [r for r in b.reasons if not r.startswith("no cross-repo work shares this ticket")]
    if b.recommendation in _QUIET:
        b.recommendation = Recommendation.REVIEW_WITH.value
        b.confidence = "low"
        b.reasons.insert(0, f"{len(found)} change(s) in other repo(s) are linked to this one by "
                            "something other than the branch name — see below for what")

def _escalate_opposite_moves(b: ImpactBrief) -> None:
    """Two changes in flight that cannot both land as they are need a decision.

    Not a review: who keeps the contract, and which change lands first, is a
    choice somebody has to make, and neither repository's tests will make it.
    """
    clashes = [i for i in b.impacts if i.conflict]
    if not clashes:
        return
    for i in clashes[:3]:
        b.reasons.insert(0, f"cannot both land as they are: {i.repo} `{i.ref}` — {i.conflict}")
    if b.recommendation in ("proceed", "review_with", "insufficient_data"):
        b.recommendation = Recommendation.DECISION_NEEDED.value
        b.confidence = "medium"


def run_brief(cfg: Config, adapters: list[VcsAdapter], repo: str, ref: str, *,
              worktree: bool = False, allow_missing_ref: bool = False,
              base: str | None = None) -> ImpactBrief:
    """Brief from a live read of the estate.

    No stored index: in-flight work is enumerated on every call and nothing is
    shared or remembered about it. The one thing kept is each repository's own
    scan, by commit, so a repository that has not moved is not read twice.
    """
    vcs = tree_reader(adapters)
    cache = ScanCache.default()
    return compute_brief(cfg, adapters, repo, ref, lambda repos: LiveIndex(vcs, repos, cache),
                         worktree=worktree, allow_missing_ref=allow_missing_ref, base=base)


# ----------------------------------------------------------------- lookups --

def match_decisions(decisions, *, ticket: str | None = None, contract: str | None = None,
                    repo: str | None = None) -> list[dict]:
    """Decisions matching a ticket, naming a contract, or living in a repo.

    Titles, status, deciders and open gates -- not bodies. The gate text is
    what tells an agent a decision is still open; the body is the document.
    """
    if not (ticket or contract or repo):
        raise BadRequest("give at least one of ticket, contract, repo")
    # A bare name, any surface: the same literal rules the brief uses to link.
    wanted = [ContractRef(sf, contract) for sf in Surface] if contract else []
    out = []
    for d in decisions:
        if repo and d.repo != repo:
            continue
        if ticket and (d.ticket or "").upper() != ticket.strip().upper() \
                and ticket.strip().upper() not in (d.title or "").upper():
            continue
        if contract and not (_names_contract(d.title, wanted)
                             or any(_names_contract(g, wanted) for g in d.open_gates)
                             or _names_contract(d.body or "", wanted)):
            continue
        out.append({"repo": d.repo, "path": d.path, "id": d.global_id, "title": d.title,
                    "source": d.source.value, "status": d.status.value, "ticket": d.ticket,
                    "deciders": list(d.deciders), "decided_on": d.when.isoformat() if d.when else None,
                    "open_gates": list(d.open_gates)})
    return out
