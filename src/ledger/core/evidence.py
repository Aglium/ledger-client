"""Choose what a repo shows the model when it introduces itself.

A card is only as good as the evidence behind it, and a repo is far too large to
send whole. So this selects: the files that say what a service *is* and what it
*exchanges*, ordered by how much they reveal per byte, truncated to a budget.

Priority is the whole design. When the budget runs out -- and on a monolith it
always does -- what survives should be the declared contracts and the route and
event definitions, not the twentieth model file. Selection is deterministic:
the same tree always yields the same evidence, so a card only changes when the
repo does.

Nothing here knows any company's layout. The categories are conventions common
across Python, PHP, TypeScript and Go estates.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from enum import IntEnum


class Priority(IntEnum):
    """Higher survives the budget. Ordered by information per byte."""

    DECLARED_CONTRACT = 100     # openapi, contract tests, schema files: unambiguous
    IDENTITY = 90               # README, package manifests: what this thing is
    MESSAGING = 80              # events, queues, topics: the cross-service surface
    ROUTES = 70                 # http handlers: the other cross-service surface
    ENTITIES = 50               # models and schemas: field names live here
    MIGRATIONS = 30             # table and column names
    OTHER = 10


_RULES: tuple[tuple[Priority, re.Pattern[str]], ...] = (
    (Priority.DECLARED_CONTRACT, re.compile(
        r"(openapi|swagger)[^/]*\.(ya?ml|json)$|asyncapi[^/]*\.(ya?ml|json)$"
        r"|\.proto$|\.avsc$|(^|/)contracts?(\.md|/)|contract[^/]*test|test[^/]*contract", re.I)),
    (Priority.IDENTITY, re.compile(
        r"^readme(\.md)?$|^[^/]*/readme\.md$|^(pyproject\.toml|setup\.cfg|composer\.json"
        r"|package\.json|go\.mod|Cargo\.toml|pom\.xml|build\.gradle[^/]*)$|^claude\.md$|^agents\.md$", re.I)),
    (Priority.MESSAGING, re.compile(
        r"(^|/)(events?|messaging|consumers?|producers?|publishers?|subscribers?|handlers?"
        r"|topics?|queues?|brokers?)(/|_|\.)|rabbit|kafka|amqp|pubsub|outbox|inbox", re.I)),
    (Priority.ROUTES, re.compile(
        r"(^|/)(api|apis|routes?|routers?|controllers?|endpoints?|resources?|views?|urls?)(/|_|\.)", re.I)),
    (Priority.ENTITIES, re.compile(
        r"(^|/)(models?|entities|entity|schemas?|domain|dto|dtos|types?)(/|_|\.)", re.I)),
    (Priority.MIGRATIONS, re.compile(r"(^|/)(migrations?|alembic|versions)/", re.I)),
)

_SKIP = re.compile(
    r"(^|/)(node_modules|vendor|\.git|\.venv|venv|dist|build|__pycache__|coverage|\.next"
    r"|htmlcov|\.mypy_cache|\.pytest_cache)/"
    r"|\.(png|jpe?g|gif|svg|ico|woff2?|ttf|eot|pdf|zip|gz|tar|jar|so|dylib|dll|lock|min\.js|map)$"
    r"|(^|/)(package-lock\.json|yarn\.lock|poetry\.lock|uv\.lock|composer\.lock)$",
    re.I,
)


def classify(path: str) -> Priority | None:
    """Priority for one path, or None when it should never be read."""
    if _SKIP.search(path):
        return None
    for priority, rx in _RULES:
        if rx.search(path):
            return priority
    return Priority.OTHER


@dataclass
class Evidence:
    repo: str
    ref: str
    files: list[tuple[str, str]] = field(default_factory=list)   # (path, body)
    tree_sample: list[str] = field(default_factory=list)
    total_files: int = 0
    skipped_unreadable: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def chars(self) -> int:
        return sum(len(b) for _, b in self.files)

    def render(self) -> str:
        """The evidence block, as the model sees it."""
        out = [f"# repository: {self.repo} @ {self.ref}", f"# files in tree: {self.total_files}", ""]
        out.append("## directory shape")
        out.extend(f"  {p}" for p in self.tree_sample)
        out.append("")
        for path, body in self.files:
            out.append(f"## file: {path}")
            out.append(body)
            out.append("")
        if self.truncated:
            out.append("## NOTE: evidence was truncated to fit the budget; "
                       "lower-priority files were omitted.")
        return "\n".join(out)


def _dir_shape(paths: list[str], limit: int = 60) -> list[str]:
    """Directories, with file counts. Cheap structural signal for a few hundred bytes."""
    counts: dict[str, int] = {}
    for p in paths:
        d = posixpath.dirname(p) or "."
        counts[d] = counts.get(d, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [f"{d}/ ({n})" for d, n in sorted(ranked, key=lambda kv: kv[0])]


def gather(
    repo: str,
    ref: str,
    tree: list[str],
    read: "callable[[str], str]",
    *,
    char_budget: int = 240_000,
    per_file_limit: int = 24_000,
    max_files: int = 120,
) -> Evidence:
    """Select and read evidence for one repo, deterministically and within budget."""
    ev = Evidence(repo=repo, ref=ref, total_files=len(tree))
    ev.tree_sample = _dir_shape(tree)

    ranked: list[tuple[int, str, str]] = []
    for path in tree:
        p = classify(path)
        if p is None:
            continue
        # Shallower paths first within a priority: `api/routes.py` says more
        # about the service than `api/v1/internal/util/routes.py`.
        ranked.append((-int(p), f"{path.count('/'):03d}", path))
    ranked.sort()

    used = 0
    for _, _, path in ranked:
        if len(ev.files) >= max_files or used >= char_budget:
            ev.truncated = True
            break
        try:
            body = read(path)
        except Exception:
            ev.skipped_unreadable.append(path)
            continue
        if not body.strip():
            continue
        if len(body) > per_file_limit:
            body = body[:per_file_limit] + f"\n... [truncated, {len(body)} chars total]"
        if used + len(body) > char_budget:
            ev.truncated = True
            continue
        ev.files.append((path, body))
        used += len(body)
    return ev
