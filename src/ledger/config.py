"""Config load. Everything that varies by company lives here, never in `core/`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ledger.core.branches import DEFAULT_RULES, BranchRules, validate_branches
from ledger.core.declarations import FORMATS
from ledger.core.gate import validate_enforce
from ledger.core.tickets import validate_keys


def validate_docs(raw) -> tuple:
    """Documentation sources: a repository in the estate, or a directory."""
    out = []
    for spec in raw or []:
        if isinstance(spec, str):
            spec = {"repo": spec}
        if not isinstance(spec, dict) or not (spec.get("repo") or spec.get("path")):
            raise ValueError(f"documentation entry needs a `repo` or a `path`: {spec!r}")
        if spec.get("repo") and spec.get("path"):
            raise ValueError(f"documentation entry has both `repo` and `path`: {spec!r}")
        out.append(spec)
    return tuple(out)


def validate_declarations(raw) -> tuple:
    """Fail at load, not halfway through a sync.

    A misspelt format silently declared nothing, and an estate with no declared
    owners looks exactly like an estate that declares none.
    """
    out = []
    for spec in raw or []:
        if not isinstance(spec, dict) or not spec.get("repo"):
            raise ValueError(f"declaration needs a `repo`: {spec!r}")
        fmt = spec.get("format")
        if fmt and fmt not in FORMATS:
            raise ValueError(f"unknown declaration format {fmt!r}; known: {', '.join(sorted(FORMATS))}")
        if not fmt and not (spec.get("produces") or spec.get("consumes")):
            raise ValueError(
                f"declaration for {spec['repo']!r} declares nothing: set `format` "
                f"({', '.join(sorted(FORMATS))}) or `produces`/`consumes` paths")
        out.append(spec)
    return tuple(out)


@dataclass(frozen=True)
class ForgeConfig:
    kind: str                       # "github" | "bitbucket"
    namespace: str                  # org or workspace
    token_env: str | None = None
    user_env: str | None = None
    password_env: str | None = None
    #: "helper" when git must use this machine's credential helper rather than
    #: the API credential above. Bitbucket Cloud needs this: an Atlassian API
    #: token reads the REST API perfectly and is rejected by git over HTTPS,
    #: which wants an app password. Without a way to say so, a working API
    #: credential silently fails every clone.
    git_credentials: str | None = None
    #: The username git should put in the URL. Not a secret -- a credential
    #: helper keys on it, and `store` returns nothing without it.
    git_user: str | None = None
    #: Who the API credential belongs to. Not a secret either, so it belongs
    #: here rather than in a `.env`, and it is what makes falling back to
    #: git's credential store safe: a developer machine holds a personal
    #: login for the same host (`jdoe` for bitbucket.org) alongside the
    #: work one, and a fallback that took whichever git offered would
    #: silently act as the wrong person against the organisation's API.
    #: Named here, a mismatched credential is refused instead.
    api_user: str | None = None


@dataclass(frozen=True)
class Config:
    org: str
    project_keys: tuple[str, ...]
    forges: list[ForgeConfig] = field(default_factory=list)
    #: The single most important scope dial in the system. It decides whether the
    #: ledger reasons over ~700 branches or ~60. Always report the resulting count
    #: before a backfill.
    liveness_days: int = 30
    #: Branches that hold integrated-but-unreleased work. Commits here that are
    #: not in the default branch are in flight: written, merged, not yet in
    #: production. Modelling only topic branches misses an entire release train
    #: -- an active integration branch routinely carries hundreds of them.
    integration_branches: tuple[str, ...] = ("stage", "staging", "develop", "release/*")
    #: Repos to leave out of the estate. The ledger's own repo belongs here when
    #: it is checked out alongside the repos it indexes: its test fixtures
    #: contain contract names, and they were showing up as real contract
    #: evidence in briefs about other repos.
    exclude_repos: tuple[str, ...] = ()
    #: Which branches count as other work in flight (`watch`, `ignore`), in
    #: GitLab's pattern syntax. See ledger.core.branches.
    branches: BranchRules = DEFAULT_RULES
    #: Whether the file said `branches` itself. Only then are the rules sent to
    #: an organisation server, which narrows its answer to them.
    branches_set: bool = False
    comment_marker: str = "ledger:siblings"
    #: Verdict classes a gate fails on. Empty: everything is advisory.
    enforce: tuple[str, ...] = ()
    #: Infrastructure-as-code that declares who produces and consumes each
    #: topic, either in a standard format ([{repo, format: strimzi}]) or in the
    #: organisation's own layout ([{repo, files, produces, consumes,
    #: produced_name, consumed_name, ignore}]). See core/declarations.py.
    declarations: tuple = ()
    #: Declared app name -> repository, where evidence cannot tell.
    app_repos: dict = field(default_factory=dict)
    #: Documentation to search for references to known contracts:
    #: [{repo | path, files, name}]. Named explicitly, because a pile of
    #: markdown is not necessarily the documentation of anything -- and a
    #: directory as well as a repository, because a mirrored wiki is
    #: routinely kept out of git.
    docs: tuple = ()
    #: Read local clones under this directory instead of forge APIs. Set here
    #: rather than per command so that an agent hook or MCP server launched by
    #: a coding tool needs only LEDGER_CONFIG. `--local-root` still overrides.
    #:
    #: This reads whatever is checked out, which is the right thing on one
    #: developer's machine and the wrong thing for a server: the estate then
    #: means "the repositories somebody happened to clone". Prefer `mirror_root`.
    local_root: str | None = None
    #: Where the server keeps its own mirrors of every repository the
    #: organisation has. It discovers them through the forges above and clones
    #: them itself, so nobody has to clone anything for it. Bare mirrors: no
    #: working trees, a fifth of the disk, every read still works.
    mirror_root: str | None = None

    @property
    def integration_names(self) -> tuple[str, ...]:
        """The integration branches named outright. Patterns (`release/*`) are
        expanded per repository where branches are enumerated; elsewhere a
        pattern names no branch and is left out."""
        return tuple(b for b in self.integration_branches if "*" not in b)

    @staticmethod
    def resolve_path(path: str | Path | None = None) -> Path:
        """Explicit path, then $LEDGER_CONFIG, then ./config/ledger.yml, then the
        user's own config, which is where `aglium-ledger init` writes one.

        The product ships only an example. A deployment's config lives with the
        deployment, so an organisation's repo names, ticket keys and forge
        namespaces never end up committed to the product.
        """
        for candidate in (path, os.environ.get("LEDGER_CONFIG"), "config/ledger.yml",
                          Config.user_path()):
            if candidate and Path(candidate).expanduser().is_file():
                return Path(candidate).expanduser()
        raise FileNotFoundError(
            "no ledger config found. Run `aglium-ledger init` inside one of your repositories, "
            "pass --config, or set LEDGER_CONFIG"
        )

    @staticmethod
    def user_path() -> Path:
        """Where one developer's config lives when nothing else says."""
        base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
        return Path(base).expanduser() / "ledger" / "ledger.yml"

    @staticmethod
    def load_secrets(config_path: Path) -> list[str]:
        """Read `.env` sitting beside the config, into the environment.

        Credentials belong with the deployment, not with the product and not
        in the config file -- which is why every one of them is named here as
        an environment variable rather than given a value. A `.env` next to
        `ledger.yml` is where those variables come from on a machine that has
        no other secret store.

        Anything already exported wins, so a shell or a service manager can
        always override the file. Returns the names it set, never the values.
        """
        env = config_path.parent / ".env"
        if not env.is_file():
            return []
        set_here: list[str] = []
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip().removeprefix("export "), value.strip().strip("'\"")
            # An empty variable is unset: a compose file passes `${TOKEN:-}` for
            # every secret it knows of, and that must not shadow the value here.
            if name and value and not os.environ.get(name):
                os.environ[name] = value
                set_here.append(name)
        return set_here

    @staticmethod
    def load(path: str | Path | None = None) -> "Config":
        resolved = Config.resolve_path(path)
        Config.load_secrets(resolved)
        raw = yaml.safe_load(resolved.read_text()) or {}
        liveness = int(raw.get("liveness_days", 30))
        if liveness < 1:
            raise ValueError("liveness_days must be >= 1")
        return Config(
            org=os.environ.get("LEDGER_ORG") or raw.get("org") or "default",
            project_keys=validate_keys(list(raw.get("project_keys") or [])),
            liveness_days=liveness,
            exclude_repos=tuple(raw.get("exclude_repos") or ()),
            branches=validate_branches(raw.get("branches")),
            branches_set="branches" in raw,
            integration_branches=tuple(raw.get("integration_branches") or ("stage", "staging", "develop", "release/*")),
            comment_marker=raw.get("comment_marker", "ledger:siblings"),
            enforce=validate_enforce(raw.get("enforce")),
            forges=[ForgeConfig(**f) for f in (raw.get("forges") or [])],
            local_root=os.environ.get("LEDGER_LOCAL_ROOT") or raw.get("local_root") or None,
            mirror_root=os.environ.get("LEDGER_MIRROR_ROOT") or raw.get("mirror_root") or None,
            declarations=validate_declarations(raw.get("declarations")),
            app_repos=dict(raw.get("app_repos") or {}),
            docs=validate_docs(raw.get("docs")),
        )
