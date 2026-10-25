"""Which branches count as somebody's change: `watch` and `ignore`, one syntax for both.

The syntax is GitLab's for protected branches, because it is the one most
teams already know: a pattern is an exact name (`main`) or a name with `*`
wildcards (`release/*`, `*-stable`, `dependabot/*`), and `*` matches any run
of characters, `/` included. Case does not matter: `Release/1.2` is `release/*`.

A branch is watched when it matches some `watch` pattern and no `ignore`
pattern; ignore wins. These rules decide what is *other work in flight*. A
repository's default branch and its `integration_branches` are never anybody's
change, whatever these lists say: they are the base, or integrated work read
commit by commit. A pull request from a fork is always watched, because its
branch name belongs to another repository. And a brief on the branch you are
standing on always runs in full.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache

#: What is ignored when a config says nothing. Long-lived lines of development
#: and release trains are not one person's change: their diff is everything
#: unreleased in the repository. Dependency bots open dozens of branches that
#: change a lock file each. `hotfix/*` is watched: a hotfix is one person's
#: urgent change, exactly the kind that collides. `main` and `master` are both listed because teams
#: differ; a repository's own default branch is skipped whatever its name.
DEFAULT_WATCH: tuple[str, ...] = ("*",)
DEFAULT_IGNORE: tuple[str, ...] = (
    "main", "master", "develop", "development", "stage", "staging", "prod", "production",
    "release/*", "land/*", "port/*",
    "dependabot/*", "renovate/*",
)


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")) + r"\Z",
                      re.IGNORECASE)


def _patterns(raw, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"branches.{key} must be a list of names or patterns, got {raw!r}")
    out = []
    for p in raw:
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"branches.{key}: every entry must be a non-empty string, got {p!r}")
        out.append(p.strip())
    return tuple(out)


@dataclass(frozen=True)
class BranchRules:
    watch: tuple[str, ...] = DEFAULT_WATCH
    ignore: tuple[str, ...] = field(default=DEFAULT_IGNORE)

    @cached_property
    def _watch(self) -> tuple[re.Pattern, ...]:
        return tuple(_compile(p) for p in self.watch)

    @cached_property
    def _ignore(self) -> tuple[re.Pattern, ...]:
        return tuple(_compile(p) for p in self.ignore)

    def ignored_by(self, name: str) -> str | None:
        """The pattern that leaves `name` out, or None when it is watched."""
        if not any(p.match(name) for p in self._watch):
            return "not in watch"
        return next((pat for pat, rx in zip(self.ignore, self._ignore) if rx.match(name)), None)

    def allows(self, name: str) -> bool:
        return self.ignored_by(name) is None

    def as_config(self) -> dict:
        return {"watch": list(self.watch), "ignore": list(self.ignore)}


DEFAULT_RULES = BranchRules()


def matches(pattern: str, name: str) -> bool:
    return bool(_compile(pattern).match(name))


def validate_branches(raw) -> BranchRules:
    """`branches: {watch: [...], ignore: [...]}`. A list given is the whole list:
    to keep the defaults and add one, copy them (`aglium-ledger init` writes them out).
    A key left out keeps its default."""
    if raw is None:
        return DEFAULT_RULES
    if not isinstance(raw, dict):
        raise ValueError(f"branches must be a mapping with `watch` and/or `ignore`, got {raw!r}")
    unknown = set(raw) - {"watch", "ignore"}
    if unknown:
        raise ValueError(f"branches: unknown key(s) {', '.join(sorted(unknown))}; "
                         "only `watch` and `ignore`")
    watch = _patterns(raw.get("watch"), "watch") if "watch" in raw else DEFAULT_WATCH
    ignore = _patterns(raw.get("ignore"), "ignore") if "ignore" in raw else DEFAULT_IGNORE
    if not watch:
        raise ValueError("branches.watch is empty, so nothing would be watched; use [\"*\"] for all")
    return BranchRules(watch, ignore)


def is_pattern(name: str) -> bool:
    return "*" in name


def any_match(patterns, name: str) -> bool:
    """Whether `name` is any of `patterns` (names or `*` patterns)."""
    return any(_compile(p).match(name) for p in patterns)


def expand_integration(entries, names) -> tuple[str, ...]:
    """`integration_branches` as concrete branch names in one repository.

    Plain names are kept as written (the branch may simply not exist there);
    patterns such as `release/*` become the live branches they match. A release
    branch is integrated-but-unreleased work, the same as `stage`, and is read
    the same way rather than as somebody's change.
    """
    out = [e for e in entries if not is_pattern(e)]
    pats = [e for e in entries if is_pattern(e)]
    if pats:
        for n in names:
            if n not in out and any_match(pats, n):
                out.append(n)
    return tuple(out)


class Patterns:
    """Names and `*` patterns as a set: `"release/2.4" in Patterns(["release/*"])`."""

    def __init__(self, entries=()):
        self.entries = tuple(entries)

    def __contains__(self, name) -> bool:
        return isinstance(name, str) and any_match(self.entries, name)

    def __bool__(self) -> bool:
        return bool(self.entries)
