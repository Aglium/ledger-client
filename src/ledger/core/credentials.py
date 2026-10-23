"""Read a forge credential from the store git already uses.

Discovery -- "what repositories does this organisation have?" -- is the one
question a disk cannot answer, and it is the only part of the pipeline that
needs a forge API credential. Everything else runs on the mirrors, where git
authenticates through this machine's credential helper.

That asymmetry is how a working installation came to have discovery
permanently unavailable. The tokens were supplied inline on the command line
for one `aglium-ledger mirrors` run, by design -- a forge token is not something to
write into a config directory. They lived in that shell and nowhere else, so
the scheduled discovery that was added later had nothing to read, said
`discover=no-credentials` every six hours, and nobody had lost anything: it
had never had any.

Meanwhile git had a perfectly good credential for both hosts the whole time.
Measured on the installation this was written for: the keychain credential
answers `GET /user` on GitHub with 200, and Bitbucket with 401 -- because
Bitbucket Cloud needs two different secrets, an app password for git over
HTTPS and an API token for the REST API, and only the git one is stored. So
this closes one of two gaps and reports the other honestly, rather than
asking for both secrets again.

Nothing here writes anything. `git credential fill` is the documented way to
ask the configured helper, it is what git itself runs, and a helper that
declines simply returns no password.
"""

from __future__ import annotations

import os
import subprocess

#: Long enough for a keychain prompt to be declined, short enough that a
#: sync is never held up by one. A helper that needs longer than this is
#: asking a human, and nothing here is running in front of one.
TIMEOUT = 5.0


def from_git(host: str, *, username: str | None = None,
             timeout: float = TIMEOUT) -> tuple[str, str] | None:
    """`(username, secret)` for a host, or None if git has nothing to offer.

    `username` is passed through when the config names one, because `store`
    returns nothing without it and a keychain may hold several.
    """
    if not host:
        return None
    query = f"protocol=https\nhost={host}\n" + (f"username={username}\n" if username else "")
    try:
        done = subprocess.run(
            ["git", "credential", "fill"], input=query + "\n", capture_output=True,
            text=True, timeout=timeout,
            # A helper must never stop to ask: this runs inside a sync loop.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    got = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition("=")
        got[key.strip()] = value
    user, secret = got.get("username"), got.get("password")
    return (user or username or "", secret) if secret else None


#: Where each forge's API lives, for asking git about it. The clone host and
#: the API host are the same name for both of these, which is why one lookup
#: serves both purposes.
HOSTS = {"github": "github.com", "bitbucket": "bitbucket.org"}


def api_host(kind: str, base_url: str | None = None) -> str:
    """The host to ask git about. A self-hosted forge is its own host."""
    if base_url:
        from urllib.parse import urlparse
        return urlparse(base_url).hostname or HOSTS.get(kind, "")
    return HOSTS.get(kind, "")
