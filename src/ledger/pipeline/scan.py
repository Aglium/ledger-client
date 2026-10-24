"""Reading one repository: its contracts and its decisions, remembered by commit.

A brief read live scans every repository in the estate, and nearly all of that
is the same work as the last time: measured on 175 repositories, 13 of a
live brief's 25 seconds went on finding contracts in repositories that had not
changed, and 2 more on their decision documents. What a repository's default
branch says only changes when that branch moves.

So a scan is remembered on disk, under the commit it was read at and the
version of the code that read it. The next brief asks each repository where
its default branch points, and reads only the ones that moved.

This is a cache, not an index. It remembers what one repository said at one
commit. It holds nothing about work in flight, nothing shared between
developers, and nothing a fresh scan would not reproduce -- which is what the
organisation server is for.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import date
from enum import Enum
from pathlib import Path

from ledger.adapters.vcs.base import VcsAdapter
from ledger.core.contracts import EXTRACTOR_VERSION, ContractRef, Surface, discover
from ledger.core.decision import PARSER_VERSION, Decision, DecisionSource, DecisionStatus
from ledger.pipeline.decisions import load_repo_decisions

#: What produced a remembered scan, beyond the commit it was read at. A scan
#: from any other version is read again: remembered must always equal fresh,
#: and an upgrade is exactly when it would not.
SCAN_VERSION = f"c{EXTRACTOR_VERSION}.d{PARSER_VERSION}.s3"   # s2: direction; s3: bound topics

#: Files kept per contract as evidence of how a repository uses it.
EVIDENCE_PATHS = 5


def scan_repo(vcs: VcsAdapter, repo: str, ref: str):
    """Read one repo once: its contract edges and its decisions."""
    tree = vcs.list_tree(repo, ref)
    disc = discover(repo, tree, None, read_many=lambda ps: vcs.read_files(repo, ref, ps))
    decisions = load_repo_decisions(vcs, repo, ref, tree=tree)
    return disc, decisions


def _greps(vcs: VcsAdapter) -> bool:
    """Can this source find an identifier without reading the whole repository?
    Local git can; a forge API would read every file, so wiring is not read there."""
    return type(vcs).grep_paths is not VcsAdapter.grep_paths


def wiring_of(vcs: VcsAdapter, repo: str, ref: str) -> tuple[dict, dict]:
    """Topics a repository reaches through a variable, and which way it uses them.

    Returns `(bound, uses)`. `bound` is `{ident: {"raws": [...], "values": [...]}}`,
    what `Index.topics_bound_in` answers. `uses` is `{ContractRef: (edge, [paths])}`
    for every bound topic, its direction read where the identifier is used --
    `producer.send(INVOICE_EVENTS_TOPIC, ...)` publishes `invoice.events.v1`
    though no line there spells it. See `core.bindings`.
    """
    from ledger.adapters.vcs.base import ForgeUnavailable
    from ledger.pipeline.wiring import repo_wiring
    if not _greps(vcs):
        return {}, {}
    try:
        bindings, usages = repo_wiring(vcs, repo, ref)
    except (ForgeUnavailable, OSError, ValueError):
        return {}, {}
    bound: dict = {}
    for b in bindings:
        v = bound.setdefault(b.ident, {"raws": set(), "values": set()})
        v["raws"].add(b.raw)
        v["values"].add(b.value)
    rank = {"publishes": 2, "consumes": 1, "mentions": 0}
    uses: dict = {}
    for ident, v in bound.items():
        mine = [u for u in usages if u.ident == ident]
        edge = max((u.edge for u in mine), key=lambda e: rank.get(e, 0), default="mentions")
        paths = list(dict.fromkeys([u.path for u in mine if u.edge == edge] + [u.path for u in mine]))
        for value in v["values"]:
            uses[ContractRef(Surface.TOPIC, value)] = (edge, paths[:EVIDENCE_PATHS])
    return ({i: {"raws": sorted(v["raws"]), "values": sorted(v["values"])} for i, v in bound.items()},
            uses)


def read_repo(vcs: VcsAdapter, repo: str, ref: str) -> tuple[dict, list, dict]:
    """`(uses, decisions, bound)` for one repository at one ref: what a scan remembers."""
    disc, decisions = scan_repo(vcs, repo, ref)
    uses = uses_of(disc)
    bound, wired = wiring_of(vcs, repo, ref)
    for c, (edge, paths) in wired.items():
        mine = uses.get(c)
        # A direction read through the binding beats a bare literal mention.
        if mine is None or (mine[0] == "mentions" and edge != "mentions"):
            uses[c] = (edge, paths or (mine[1] if mine else []))
    return uses, decisions, bound


def uses_of(disc) -> dict:
    """Which way a repository faces each contract it names, and where it says so.

    `{ContractRef: (edge, [paths])}`. One direction per contract, collapsed the
    way `resolve_direction` does: a repository that publishes a topic and also
    reads it back in its own tests is a publisher.
    """
    from ledger.core.contracts import resolve_direction
    out: dict = {}
    for c, edges in disc.by_contract().items():
        edge = resolve_direction(edges)
        # Evidence for the direction first: the line that says which way.
        paths = [e.evidence_path for e in edges if e.edge == edge] + [e.evidence_path for e in edges]
        out[c] = (edge.value, list(dict.fromkeys(paths))[:EVIDENCE_PATHS])
    return out


def _plain(v):
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, date):
        return v.isoformat()
    return v


def _decision_out(d: Decision) -> dict:
    return {f.name: _plain(getattr(d, f.name)) for f in dataclasses.fields(d)}


def _decision_in(raw: dict) -> Decision:
    known = {f.name for f in dataclasses.fields(Decision)}
    got = {k: v for k, v in raw.items() if k in known}
    got["source"] = DecisionSource(got["source"])
    got["status"] = DecisionStatus(got["status"])
    if got.get("when"):
        got["when"] = date.fromisoformat(got["when"])
    return Decision(**got)


class ScanCache:
    """Scans kept on disk, one file per repository.

    Every failure to read or write the cache is a fresh scan, never an error:
    a cache that can break an answer is worse than none.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.hits = 0
        self.misses = 0

    @classmethod
    def default(cls) -> "ScanCache | None":
        """`$LEDGER_CACHE_DIR`, or the user's cache directory. `off` disables it."""
        where = os.environ.get("LEDGER_CACHE_DIR")
        if where and where.lower() in ("off", "none", "0"):
            return None
        return cls(where or Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache") / "ledger" / "scan")

    def _file(self, repo: str) -> Path:
        return self.root / (hashlib.sha256(repo.encode()).hexdigest()[:24] + ".json")

    def get(self, vcs: VcsAdapter, repo: str, ref: str) -> tuple[set[ContractRef], list[Decision]]:
        """Contracts and decisions of `repo` at `ref`, scanning only if it moved.

        Raises ForgeUnavailable exactly as a scan would: an unreadable
        repository is unknown, and nothing remembered may stand in for it.
        """
        uses, decisions, _ = self._record(vcs, repo, ref)
        return set(uses), decisions

    def uses(self, vcs: VcsAdapter, repo: str, ref: str) -> dict:
        """`{ContractRef: (edge, [paths])}`: which way `repo` faces each contract."""
        return self._record(vcs, repo, ref)[0]

    def bound(self, vcs: VcsAdapter, repo: str, ref: str) -> dict:
        """`{ident: {"raws", "values"}}`: topics `repo` binds to a variable."""
        return self._record(vcs, repo, ref)[2]

    def _record(self, vcs: VcsAdapter, repo: str, ref: str) -> tuple[dict, list[Decision], dict]:
        sha = vcs.head_sha(repo, ref)
        f = self._file(repo)
        try:
            raw = json.loads(f.read_text())
            if (raw["repo"], raw["sha"], raw["version"]) == (repo, sha, SCAN_VERSION):
                self.hits += 1
                return ({ContractRef(Surface(s), n): (e, list(ps)) for s, n, e, ps in raw["contracts"]},
                        [_decision_in(d) for d in raw["decisions"]], raw.get("bound") or {})
        except Exception:                               # noqa: BLE001 -- absent or unreadable: scan
            pass
        self.misses += 1
        uses, decisions, bound = read_repo(vcs, repo, ref)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({
                "repo": repo, "sha": sha, "version": SCAN_VERSION,
                "contracts": sorted([c.surface.value, c.name, e, ps] for c, (e, ps) in uses.items()),
                "decisions": [_decision_out(d) for d in decisions], "bound": bound}))
            tmp.replace(f)                              # whole file or none: briefs run concurrently
        except Exception:                               # noqa: BLE001 -- not remembered, still answered
            pass
        return uses, decisions, bound
