"""A first config, derived from the checkout a developer is already in.

The first answer has to arrive before anyone has read a config reference.
Everything a brief needs to start is already on the machine: the origin remote
says which forge and which organisation, the branches and commit subjects say
which ticket keys the team uses, and the checkouts next door are the estate
this developer works in.

Nothing here contacts a forge or writes anything; `aglium-ledger init` does the
writing. What cannot be derived is left out rather than guessed, and said.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ledger.core.branches import DEFAULT_IGNORE, DEFAULT_WATCH, matches

#: host -> the forge adapter that reads it. A forge not listed here has no
#: adapter, and a config naming it would fail at the first command.
FORGES = {"github.com": "github", "bitbucket.org": "bitbucket"}

_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)(?P<host>[^/:]+)[:/]"
    r"(?P<namespace>.+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,6})(?![A-Za-z0-9])")

#: Shaped like a ticket key and not one. `KEY-0` style placeholders are
#: dropped separately, by number.
NOT_A_KEY = frozenset({
    "UTF", "ISO", "SHA", "MD", "CVE", "RFC", "HTTP", "HTTPS", "TLS", "SSL", "V", "VERSION",
    "PYTHON", "PY", "NODE", "PHP", "JAVA", "GO", "ES", "X", "RC", "ALPHA", "BETA", "PR", "MR",
    "STEP", "PHASE", "PART", "WEEK", "DAY", "Q", "H", "BASE", "COVID", "GPT", "AES", "RSA",
    # Branch vocabulary: `stage-2`, `release-14`, `hotfix-3`.
    "STAGE", "STAGING", "RELEASE", "HOTFIX", "DEV", "DEVELOP", "PROD", "MAIN", "MASTER",
    "FEATURE", "FEAT", "FIX", "BUGFIX", "TEST", "TESTS", "DRAFT", "WIP", "SPRINT", "BACKUP",
})

#: A key has to recur to be the team's convention rather than a stray match.
MIN_KEY_USES = 3
INTEGRATION_CANDIDATES = ("stage", "staging", "develop")

#: Bots that open branches nobody wrote, added to `ignore` only when this
#: machine has one: the defaults already cover Dependabot and Renovate.
BOT_PATTERNS = ("snyk-*", "greenkeeper/*", "pre-commit-ci-*", "imgbot", "deepsource-*")


def branch_names(repo: Path) -> list[str]:
    """Local and remote-tracking branch names, without `refs/heads/` or `origin/`."""
    out = []
    for ref in _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads",
                    "refs/remotes").splitlines():
        if ref.startswith("refs/heads/"):
            out.append(ref[len("refs/heads/"):])
        elif ref.startswith("refs/remotes/"):
            name = ref[len("refs/remotes/"):].partition("/")[2]
            if name and name != "HEAD":
                out.append(name)
    return out


def branch_rules(names, *, top: int = 5) -> tuple[dict, str | None]:
    """The `branches` block to write, and a note on what the branches here look like."""
    ignore = list(DEFAULT_IGNORE)
    for bot in BOT_PATTERNS:
        if any(matches(bot, n) for n in names):
            ignore.append(bot)
    prefixes: Counter = Counter(n.split("/", 1)[0] + "/*" for n in set(names) if "/" in n)
    if not prefixes:
        return {"watch": list(DEFAULT_WATCH), "ignore": ignore}, None
    shown = ", ".join(f"{p} ({n}{', ignored' if any(matches(i, p[:-1] + 'x') for i in ignore) else ''})"
                      for p, n in prefixes.most_common(top))
    note = (f"Branch names here start with {shown}. Every branch is watched except those under "
            "`branches.ignore`; to watch only some, list them under `branches.watch` "
            "(exact names or `*` patterns, as in GitLab).")
    return {"watch": list(DEFAULT_WATCH), "ignore": ignore}, note


@dataclass
class Remote:
    host: str
    namespace: str
    repo: str

    @property
    def kind(self) -> str | None:
        return FORGES.get(self.host.lower())


def parse_remote(url: str) -> Remote | None:
    """`git@github.com:acme/orders.git` and its https and ssh:// spellings."""
    m = _REMOTE.match((url or "").strip())
    if not m:
        return None
    return Remote(m.group("host"), m.group("namespace"), m.group("repo"))


def guess_keys(texts, *, min_uses: int = MIN_KEY_USES, limit: int = 6) -> list[str]:
    """Ticket keys the team uses, from branch names and commit subjects.

    Case-insensitive, because branches are lower-cased by habit
    (`feat/abc-123-export`). A key counts once per text it appears in, so one
    long branch name cannot vote several times.
    """
    seen: Counter = Counter()
    for t in texts:
        found = {m.group(1).upper() for m in _KEY.finditer(t or "") if int(m.group(2)) > 0}
        seen.update(k for k in found if k not in NOT_A_KEY and not k.isdigit())
    return [k for k, n in seen.most_common(limit) if n >= min_uses]


def _git(path: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def checkout_root(start: Path) -> Path | None:
    top = _git(start, "rev-parse", "--show-toplevel").strip()
    return Path(top) if top else None


def sibling_checkouts(root: Path) -> list[Path]:
    """Other git checkouts in the directory this one sits in."""
    try:
        return sorted(p for p in root.parent.iterdir()
                      if p != root and p.is_dir() and (p / ".git").exists())
    except OSError:
        return []


@dataclass
class Plan:
    """What was derived, and what could not be."""

    config: dict
    notes: list[str] = field(default_factory=list)
    remote: Remote | None = None

    def render(self) -> str:
        head = ("# Written by `aglium-ledger init` from the checkout it was run in. Every value is a\n"
                "# guess from what was on this machine: read it once and correct it.\n"
                "# Reference: config/ledger.example.yml in the ledger repository.\n\n")
        return head + yaml.safe_dump(self.config, sort_keys=False, default_flow_style=None)


def plan(start: str | Path, *, mirror_base: str = "~/.ledger/mirrors") -> Plan:
    """Derive a config from the checkout at `start`. Reads git; changes nothing."""
    root = checkout_root(Path(start).expanduser())
    if root is None:
        raise ValueError(f"{start} is not inside a git checkout: run `aglium-ledger init` from one "
                         "of your organisation's repositories")
    notes: list[str] = []
    remote = parse_remote(_git(root, "remote", "get-url", "origin"))
    siblings = sibling_checkouts(root)

    texts: list[str] = []
    names: list[str] = []
    for repo in [root, *siblings[:30]]:
        here = branch_names(repo)
        names += here
        texts += here
        texts += _git(repo, "log", "-400", "--all", "--format=%s").splitlines()
    keys = guess_keys(texts)
    if not keys:
        notes.append("No ticket key recurs in branch names or commit subjects here. Work is then "
                     "linked only by what it touches; add `project_keys` if your team uses them.")

    branches = set(_git(root, "for-each-ref", "--format=%(refname:short)",
                        "refs/remotes/origin").splitlines())
    integration = [b for b in INTEGRATION_CANDIDATES if f"origin/{b}" in branches]

    cfg: dict = {"org": (remote.namespace.split("/")[0] if remote else root.parent.name),
                 "project_keys": keys, "liveness_days": 30,
                 "integration_branches": integration or list(INTEGRATION_CANDIDATES)}
    cfg["branches"], branch_note = branch_rules(names)
    if branch_note:
        notes.append(branch_note)
    if siblings:
        cfg["local_root"] = str(root.parent)
    else:
        notes.append("No other checkout sits beside this one, so the estate is only what gets "
                     "mirrored. Run `aglium-ledger mirrors` before the first brief.")

    if remote is None:
        notes.append("This checkout has no origin remote, so no forge is configured: the estate "
                     "is the checkouts on this machine and nothing else.")
    elif remote.kind is None:
        notes.append(f"{remote.host} has no adapter yet (GitHub and Bitbucket Cloud do), so the "
                     "organisation's repositories cannot be discovered. The estate is the "
                     "checkouts on this machine.")
    else:
        forge: dict = {"kind": remote.kind, "namespace": remote.namespace.split("/")[0]}
        if remote.kind == "github":
            forge["token_env"] = "LEDGER_GITHUB_TOKEN"
        else:
            forge.update(user_env="LEDGER_BITBUCKET_USER",
                         password_env="LEDGER_BITBUCKET_APP_PASSWORD", git_credentials="helper")
        cfg["forges"] = [forge]
        cfg["mirror_root"] = f"{mirror_base.rstrip('/')}/{forge['namespace']}"
        notes.append(f"`aglium-ledger mirrors` will clone {forge['namespace']}'s repositories into "
                     f"{cfg['mirror_root']} as bare mirrors. It uses the credential git already "
                     f"has for {remote.host} unless the variables named under `forges` are set.")
    if "local_root" not in cfg and "mirror_root" not in cfg:
        cfg["local_root"] = str(root.parent)
    return Plan(cfg, notes, remote)
