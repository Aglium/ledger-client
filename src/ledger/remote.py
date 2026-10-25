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
_preferences: dict[str, str] = {}


def use_config(cfg) -> None:
    """Send this config's preferences with every request from now on."""
    _preferences.clear()
    if getattr(cfg, "branches_set", False):
        _preferences[BRANCHES_HEADER] = json.dumps(cfg.branches.as_config(),
                                                   separators=(",", ":"))


def decorate(req: urllib.request.Request) -> urllib.request.Request:
    for name, value in _preferences.items():
        req.add_header(name, value)
    return req


def _send(req: urllib.request.Request, token: str | None, timeout: float) -> dict:
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    decorate(req)
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
        timeout: float = TIMEOUT) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return _send(urllib.request.Request(f"{url.rstrip('/')}{path}?{q}"), token, timeout)


def post(url: str, token: str | None, path: str, body: dict, *,
         timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(f"{url.rstrip('/')}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return _send(req, token, timeout)
