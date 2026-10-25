"""Asking the organisation server.

The client never holds the stored index. Where an organisation runs a server,
the client asks it over HTTP and gets back the same derived facts every other
surface hands out. Standard library only, like the server it talks to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from ledger.service import LedgerError, Unavailable

TIMEOUT = 20.0

#: This developer's own `branches` rules, sent with every question. The
#: organisation's rules on the server still decide what is indexed and what a
#: gate sees; these can only take away from an answer, never add to it. A
#: server that predates them ignores the header, and the answer is unnarrowed.
BRANCHES_HEADER = "X-Ledger-Branches"


def headers_for(cfg) -> dict[str, str]:
    """What a config asks every request to carry. Passed explicitly by each
    caller: nothing about one config outlives the call that read it."""
    if cfg is None or not getattr(cfg, "branches_set", False):
        return {}
    return {BRANCHES_HEADER: json.dumps(cfg.branches.as_config(), separators=(",", ":"))}


def _send(req: urllib.request.Request, token: str | None, timeout: float,
          headers: dict | None = None) -> dict:
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # The server answers errors as {"error": ...}; pass its words on.
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except Exception:                               # noqa: BLE001 -- not JSON, not ours
            msg = str(e)
        raise LedgerError(msg) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unavailable(f"organisation server unreachable: {e}") from None


def get(url: str, token: str | None, path: str, params: dict, *,
        timeout: float = TIMEOUT, headers: dict | None = None) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return _send(urllib.request.Request(f"{url.rstrip('/')}{path}?{q}"), token, timeout, headers)


def post(url: str, token: str | None, path: str, body: dict, *,
         timeout: float = TIMEOUT, headers: dict | None = None) -> dict:
    req = urllib.request.Request(f"{url.rstrip('/')}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return _send(req, token, timeout, headers)


#: A brief can take a while on a large estate; the default is for lookups.
BRIEF_TIMEOUT = 60.0


def same_machine(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def brief_asker(cfg, url: str, token: str | None, *, via: str, timeout: float = BRIEF_TIMEOUT,
                worktree: bool = False, allow_missing_ref: bool = False,
                headers: dict | None = None):
    """`(repo, ref, base=None) -> dict`: a brief from the organisation server.

    A server on this machine reads the same checkout this runs in, so it is
    simply asked, worktree and all. A server anywhere else has only mirrors:
    it cannot see commits that are not pushed or files that are not
    committed, which is the state a change is in when a brief is wanted. So
    this machine reads its own change and sends it. If it cannot -- no config
    here, the repository is not checked out, or the change is too large to
    send -- the server answers from what it can see.

    One implementation for the CLI, the hooks and the MCP tools, so they cannot
    drift apart.
    """
    from ledger import service
    from ledger.surfaces.hook import http_brief

    local_server = same_machine(url)
    ask = http_brief(url, token, timeout, via=via, worktree=worktree and local_server,
                     allow_missing_ref=allow_missing_ref, headers=headers)
    if local_server or allow_missing_ref or cfg is None:
        return ask

    def brief_for(repo: str, ref: str, base: str | None = None) -> dict:
        try:
            vcs = service.tree_reader(service.build_adapters(cfg, None))
            wire = service.read_local_change(cfg, vcs, repo, ref, worktree=worktree,
                                             base=base).to_wire()
        except Exception:                               # noqa: BLE001 -- not readable here: ask plainly
            wire = None
        if wire is None:
            return ask(repo, ref, base)
        return post(url, token, "/brief", {"repo": repo, "ref": ref, "via": via, "local": wire},
                    timeout=timeout, headers=headers)
    return brief_for
