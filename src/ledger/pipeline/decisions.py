"""Load Layer 1 decisions from indexed repos."""

from __future__ import annotations

from dataclasses import replace

from ledger.adapters.vcs.base import ForgeUnavailable, VcsAdapter
from ledger.core.decision import Decision, parse_adr, parse_todos

ADR_DIRS = ("docs/adr/", "docs/adrs/", "docs/decisions/", "decisions/")
TODO_FILES = ("TODOS.md", "TODO.md", "docs/TODOS.md")


def _decision_paths(tree: list[str]) -> tuple[list[str], list[str]]:
    adrs, todos = [], []
    for path in tree:
        low = path.lower()
        if any(low.startswith(d) for d in ADR_DIRS) and low.endswith(".md"):
            if low.rsplit("/", 1)[-1] not in ("readme.md", "index.md"):
                adrs.append(path)
        elif path in TODO_FILES:
            todos.append(path)
    return adrs, todos


def load_repo_decisions(
    vcs: VcsAdapter, repo: str, ref: str, tree: list[str] | None = None
) -> list[Decision]:
    """Decisions for one repo. Raises ForgeUnavailable if the tree is unreadable."""
    if tree is None:
        tree = vcs.list_tree(repo, ref)
    adrs, todos = _decision_paths(tree)
    texts = vcs.read_files(repo, ref, adrs + todos)
    out: list[Decision] = []
    for path in adrs:
        if path in texts and (d := parse_adr(repo, path, texts[path])):
            out.append(d)
    for path in todos:
        if path in texts:
            out.extend(parse_todos(repo, path, texts[path]))
    return out


def load_branch_decisions(
    vcs: VcsAdapter, repo: str, base: str, ref: str
) -> list[Decision]:
    """Decision documents a branch adds or edits, read at that branch.

    Only the files the branch actually touches, so this costs one diff per
    branch rather than a tree read. Each decision carries `ref`, marking it as
    one being made right now rather than one already settled on the default
    branch.
    """
    if not vcs.supports_diffs:
        return []
    paths = vcs.changed_paths(repo, base, ref)
    adrs, todos = _decision_paths(paths)
    if not (adrs or todos):
        return []
    texts = vcs.read_files(repo, ref, adrs + todos)
    out: list[Decision] = []
    for path in adrs:
        if path in texts and (d := parse_adr(repo, path, texts[path])):
            out.append(replace(d, ref=ref))
    for path in todos:
        if path in texts:
            out.extend(replace(d, ref=ref) for d in parse_todos(repo, path, texts[path]))
    return out


def load_decisions(vcs: VcsAdapter, repos: dict[str, str]) -> tuple[list[Decision], list[str]]:
    """Returns (decisions, unreadable_repos). Unreadable is reported, never silent."""
    out: list[Decision] = []
    unreadable: list[str] = []
    for repo, ref in repos.items():
        try:
            out.extend(load_repo_decisions(vcs, repo, ref))
        except ForgeUnavailable:
            unreadable.append(repo)
    return out, unreadable
