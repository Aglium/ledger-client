"""MCP surface: the ledger as tools a coding agent calls while it works.

An agent in one repo cannot see the others; these tools are how it asks. Every
tool returns derived facts -- repo and branch names, contract names, decision
titles, gates, paths as evidence -- never file contents, so granting an agent
this server does not grant it the estate's code.

    brief(repo, ref)                  what a change affects, and what to do
    inflight_for(ticket)              open work on a ticket, in every repo
    who_owns(surface, name)           repos that touch a contract
    decisions_for(ticket|contract|repo)  ADRs and TODOs that govern it
    feedback(verdict, ...)            useful, already known, false positive, can't verify -- or missed?

The tools are the same wherever the answer comes from. A *backend* supplies
it: the organisation server over HTTP, or a live read of the estate on this
machine. The server package adds a third, reading its database directly.

Requires the `mcp` extra:  uv pip install 'aglium-ledger[mcp]'
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from ledger import feedback as fb
from ledger import remote, service
from ledger.config import Config
from ledger.core.contracts import ContractRef, Surface
from ledger.pipeline.collect import collect
from ledger.pipeline.decisions import load_decisions
from ledger.pipeline.scan import ScanCache
from ledger.service import BadRequest, LedgerError

INSTRUCTIONS = """\
Cross-repo context for the change you are working on. Your repo is one of many;
these tools see all of them, including work in flight on other branches.

Call `brief` before proposing or reviewing a change to a branch: it says which
other repos and in-flight branches the change affects and ends in a
recommendation -- proceed, review_with, decision_needed or insufficient_data.
Treat decision_needed as "a person with authority must decide first" and name
the deciders it lists. Treat insufficient_data as "unknown", never as "safe".

Use `inflight_for` when a ticket is mentioned, `who_owns` before changing a
topic, endpoint or table, and `decisions_for` before contradicting a design.

When the user reacts to something the ledger said -- "that was useful", "we
already knew", "that's wrong", "can't tell from this", or "it should have told
me about X" -- record it with `feedback`. Do not ask for feedback unprompted;
record it when it is given. Without a server it is kept on this machine only.
"""

#: Said on every answer read without the organisation index, because a short
#: answer from a live read and a short answer from a full index look the same.
LIVE_NOTE = ("Read live on this machine, without the organisation index: no declared or bound "
             "topic wiring, no field-level contracts, no documentation links, and nothing "
             "remembered between calls. `aglium-ledger server` says what an organisation "
             "server adds and where to get one.")


class Backend(Protocol):
    """Where the answers come from."""

    def brief(self, repo: str, ref: str) -> dict: ...
    def inflight_for(self, ticket: str) -> dict: ...
    def who_owns(self, surface: str, name: str) -> dict: ...
    def decisions_for(self, ticket: str | None, contract: str | None, repo: str | None) -> dict: ...
    def feedback(self, verdict: str, note: str, event_id: str | None, repo: str | None,
                 ref: str | None, tool: str | None) -> dict: ...


class RemoteBackend:
    """The organisation server, over HTTP."""

    def __init__(self, url: str, token: str | None):
        self.url, self.token = url, token

    def _get(self, path: str, **params) -> dict:
        return remote.get(self.url, self.token, path, {**params, "via": "mcp"})

    def brief(self, repo, ref):
        return self._get("/brief", repo=repo, ref=ref)

    def inflight_for(self, ticket):
        return self._get("/inflight", ticket=ticket)

    def who_owns(self, surface, name):
        return self._get("/owners", surface=surface, name=name)

    def decisions_for(self, ticket, contract, repo):
        return self._get("/decisions", ticket=ticket, contract=contract, repo=repo)

    def feedback(self, verdict, note, event_id, repo, ref, tool):
        try:
            v = fb.normalise(verdict)
        except fb.BadVerdict as e:
            raise BadRequest(str(e)) from None
        word = fb.SERVER_WORD[v]
        if word != v:
            # The server's own words are coarser; the reader's word goes with the note.
            note = f"[{v}] {note}".strip()
        return remote.post(self.url, self.token, "/feedback",
                           {"verdict": word, "note": note, "event_id": event_id,
                            "repo": repo, "ref": ref, "tool": tool})


class LiveBackend:
    """No server: read the estate on this machine, for this call."""

    def __init__(self, cfg: Config, adapters: Callable[[], list]):
        self.cfg, self.adapters = cfg, adapters

    def brief(self, repo, ref):
        return service.run_brief(self.cfg, self.adapters(), repo, ref).to_dict()

    def inflight_for(self, ticket):
        ticket = ticket.strip().upper()
        cfg = self.cfg
        got = collect(self.adapters(), cfg.project_keys, liveness_days=cfg.liveness_days,
                      integration_branches=cfg.integration_branches,
                      exclude_repos=cfg.exclude_repos,
                      branches=cfg.branches)
        changes = [{
            "repo": c.repo, "ref": c.ref, "kind": c.kind, "pr_number": c.pr_number,
            "title": c.title, "author": c.author, "url": c.url,
            "where": c.integration_ref if c.kind == "integration" else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        } for c in got.changes if ticket in c.tickets]
        notes = [LIVE_NOTE]
        if got.partial:
            notes.insert(0, "PARTIAL: " + ", ".join(got.missing_sources) + " could not be read, "
                            "so work there is unknown rather than absent.")
        return {"ticket": ticket, "repos": sorted({c["repo"] for c in changes}),
                "changes": changes, "notes": notes,
                "freshness": {"synced_at": None, "age_seconds": 0, "partial": got.partial}}

    def who_owns(self, surface, name):
        try:
            c = ContractRef(Surface(surface), name)
        except ValueError:
            raise BadRequest(f"unknown surface {surface!r}; one of "
                             f"{', '.join(x.value for x in Surface)}") from None
        vcs = service.tree_reader(self.adapters())
        idx = service.LiveIndex(vcs, service.repos_for(self.cfg, vcs), ScanCache.default())
        repos = idx.owners_for([c]).get(c, [])
        notes = [LIVE_NOTE, "Each repository below names this contract in its source. That is not "
                            "evidence of which one produces or consumes it."]
        if not repos:
            notes.insert(0, "No repository was found naming this contract on its default branch. "
                            "Treat this as NOT FOUND, not as unused.")
        return {"contract": {"surface": c.surface.value, "name": c.name},
                "summary": f"{c.surface.value} {c.name}: named in {', '.join(repos) or 'no repository'}.",
                "repos": [{"repo": r, "uses": "mentions", "how": "literal"} for r in repos],
                "notes": notes,
                "index": {"source": "live", "live_repos": idx.live_repos,
                          "unreadable_repos": sorted(idx.unreadable)}}

    def decisions_for(self, ticket, contract, repo):
        vcs = service.tree_reader(self.adapters())
        repos = service.repos_for(self.cfg, vcs)
        decisions, unreadable = load_decisions(vcs, repos)
        out = service.match_decisions(decisions, ticket=ticket, contract=contract, repo=repo)
        return {"query": {"ticket": ticket, "contract": contract, "repo": repo}, "decisions": out,
                "notes": [LIVE_NOTE + " Decisions still on unmerged branches are not included."],
                "index": {"source": "live", "live_repos": sorted(repos),
                          "unreadable_repos": sorted(unreadable)}}

    def feedback(self, verdict, note, event_id, repo, ref, tool):
        """No server: kept in a file on this machine, and sent nowhere."""
        try:
            row = fb.record(verdict, repo=repo, ref=ref, note=note, source=f"mcp:{tool or ''}",
                            event_id=event_id)
        except fb.BadVerdict as e:
            raise BadRequest(str(e)) from None
        return {"verdict": row["verdict"], "kept": True, "where": str(fb.store()),
                "note": "Kept on this machine only; nothing was sent anywhere. "
                        "`aglium-ledger feedback summary` shows the totals."}


NOT_SET_UP = ("Ledger is not set up on this machine yet. Run /ledger:setup in Claude Code, or "
              "`aglium-ledger init` inside one of your organisation's repositories, then ask again.")


class SetupBackend:
    """Reads the config on first use, not at start.

    A plugin starts its MCP server as soon as it is installed, before anyone has
    run setup. Starting without a config keeps the server up, answers each tool
    with what to do, and picks the config up once setup has written it.
    """

    def __init__(self, load: Callable[[], Config], make: Callable[[Config], Backend]):
        self.load, self.make, self.backend = load, make, None

    def _ready(self) -> Backend:
        if self.backend is None:
            try:
                cfg = self.load()
            except FileNotFoundError:
                raise LedgerError(NOT_SET_UP) from None
            self.backend = self.make(cfg)
        return self.backend

    def brief(self, repo, ref):
        return self._ready().brief(repo, ref)

    def inflight_for(self, ticket):
        return self._ready().inflight_for(ticket)

    def who_owns(self, surface, name):
        return self._ready().who_owns(surface, name)

    def decisions_for(self, ticket, contract, repo):
        return self._ready().decisions_for(ticket, contract, repo)

    def feedback(self, verdict, note, event_id, repo, ref, tool):
        return self._ready().feedback(verdict, note, event_id, repo, ref, tool)


def _call(fn: Callable[[], Any]) -> Any:
    """Tool errors as data. An agent reading {"error": ...} can correct its
    input; an exception becomes a generic failure it cannot act on."""
    try:
        return fn()
    except LedgerError as e:
        return {"error": str(e)}


def build_server(backend: Backend):
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as e:        # pragma: no cover -- exercised by the CLI message
        raise LedgerError("the MCP server needs the `mcp` extra: uv pip install 'aglium-ledger[mcp]'") from e

    from importlib.metadata import PackageNotFoundError, version
    try:
        v = version("aglium-ledger")
    except PackageNotFoundError:
        v = "0"
    srv = MCPServer(name="ledger", version=v, instructions=INSTRUCTIONS)

    @srv.tool()
    def brief(repo: str, ref: str) -> dict:
        """Impact brief for a branch: affected repos and in-flight work, contracts
        it shares with other repos, governing decisions, and a recommendation.
        Each in-flight item says what it does (`effect`), how it faces this
        change (`relation`), how far to trust that (`evidence`) and what to
        ask its author (`ask`) -- reason from those, not from its branch name.
        `behaviour`, when present, is a model's summary of that change's diff:
        a lead to verify, never a fact to report."""
        return _call(lambda: backend.brief(repo, ref))

    @srv.tool()
    def inflight_for(ticket: str) -> dict:
        """Open branches, PRs and integrated-but-unreleased commits carrying a
        ticket key (e.g. ABC-123), across every repo. Includes the index age.
        Where the server can read a change's diff, the change carries `effect`
        (what it adds, removes and changes) and `evidence` (how far to trust it)."""
        return _call(lambda: backend.inflight_for(ticket))

    @srv.tool()
    def who_owns(surface: str, name: str) -> dict:
        """Repos that touch a contract. surface is one of: topic, http, table.
        Example: who_owns("topic", "order.events.v1")."""
        return _call(lambda: backend.who_owns(surface, name))

    @srv.tool()
    def decisions_for(ticket: str | None = None, contract: str | None = None,
                      repo: str | None = None) -> dict:
        """ADRs and TODO entries matching a ticket, naming a contract, or living in
        a repo. Returns status, deciders and open gates -- an open gate means the
        decision is not finished and someone must still decide."""
        return _call(lambda: backend.decisions_for(ticket, contract, repo))

    @srv.tool()
    def feedback(verdict: str, note: str = "", event_id: str | None = None,
                 repo: str | None = None, ref: str | None = None, tool: str | None = None) -> dict:
        """Record what the user thought of a ledger answer.

        verdict: useful | known | false_positive | insufficient_evidence | missed.
        `known`: right, but the user already knew. `insufficient_evidence`: the
        user could not check it from what was given. `missed`: the ledger
        should have said something and did not -- describe what in `note`.
        Without an organisation server the answer is kept on this machine only.
        Pass the `event_id` from the answer -- every answer carries one. Without
        it, pass the repo and the `tool` that answered (brief, who_owns, ...),
        so the verdict lands on the right answer.
        """
        return _call(lambda: backend.feedback(verdict, note, event_id, repo, ref, tool))

    return srv
