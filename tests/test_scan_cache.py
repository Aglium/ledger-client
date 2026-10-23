"""A repository that has not moved is not read twice -- and one that has, is."""

from __future__ import annotations

import pytest

from ledger import service
from ledger.adapters.vcs.base import BranchRef, ForgeUnavailable, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.core.contracts import ContractRef, Surface
from ledger.pipeline import scan
from ledger.pipeline.scan import ScanCache

ADR = """---
id: "0001"
title: The order_line table is owned by billing
status: accepted
ticket: ABC-1
deciders: [Alice Example]
date: 2026-03-01
---
# ADR 1
The remaining gate is that order_line still lacks a backfill for old rows.
"""
TREE = {
    "billing": {"app/db.py": "__tablename__ = 'order_line'\n", "docs/adr/0001-order-line.md": ADR},
    "orders": {"app/models.py": "__tablename__ = 'order_line'\n"},
}
ORDER_LINE = ContractRef(Surface.TABLE, "order_line")


class Counting(FakeAdapter):
    """Counts tree reads: one per scan, none for a remembered one."""

    def __init__(self, heads=None, tree=None):
        heads = heads or {r: "a" * 40 for r in TREE}
        super().__init__(
            repos=[RepoRef(full_name=r, default_branch="main", host="fake") for r in TREE],
            branches={r: [BranchRef(repo=r, name="main", head_sha=heads[r])] for r in TREE},
            tree={r: dict(files) for r, files in (tree or TREE).items()})
        self.scans = 0

    def list_tree(self, repo, ref):
        self.scans += 1
        return super().list_tree(repo, ref)


REPOS = {"billing": "main", "orders": "main"}


class TestAScanIsRememberedByCommit:
    def test_the_second_brief_reads_nothing_again(self, tmp_path):
        cache = ScanCache(tmp_path)
        first = Counting()
        assert service.LiveIndex(first, REPOS, cache).owners_for([ORDER_LINE]) == {
            ORDER_LINE: ["billing", "orders"]}
        assert first.scans == 2
        again = Counting()
        assert service.LiveIndex(again, REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE]) == {
            ORDER_LINE: ["billing", "orders"]}
        assert again.scans == 0

    def test_a_repository_that_moved_is_read_again_and_only_that_one(self, tmp_path):
        service.LiveIndex(Counting(), REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE])
        moved = Counting(heads={"billing": "a" * 40, "orders": "b" * 40},
                         tree={**TREE, "orders": {"app/models.py": "__tablename__ = 'refund'\n"}})
        got = service.LiveIndex(moved, REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE])
        assert got == {ORDER_LINE: ["billing"]} and moved.scans == 1

    def test_decisions_come_back_as_they_went_in(self, tmp_path):
        fresh, _ = service.LiveIndex(Counting(), REPOS).decisions()
        service.LiveIndex(Counting(), REPOS, ScanCache(tmp_path)).decisions()
        vcs = Counting()
        remembered, unreadable = service.LiveIndex(vcs, REPOS, ScanCache(tmp_path)).decisions()
        assert vcs.scans == 0 and unreadable == []
        assert remembered == fresh and remembered[0].when.isoformat() == "2026-03-01"

    def test_one_scan_serves_both_questions(self, tmp_path):
        vcs = Counting()
        idx = service.LiveIndex(vcs, REPOS, ScanCache(tmp_path))
        idx.decisions()
        idx.owners_for([ORDER_LINE])
        assert vcs.scans == 2

    def test_an_upgrade_reads_everything_again(self, tmp_path, monkeypatch):
        service.LiveIndex(Counting(), REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE])
        monkeypatch.setattr(scan, "SCAN_VERSION", "next")
        vcs = Counting()
        service.LiveIndex(vcs, REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE])
        assert vcs.scans == 2

    def test_a_damaged_file_is_a_scan_not_an_error(self, tmp_path):
        cache = ScanCache(tmp_path)
        service.LiveIndex(Counting(), REPOS, cache).owners_for([ORDER_LINE])
        for f in tmp_path.iterdir():
            f.write_text("{not json")
        vcs = Counting()
        assert service.LiveIndex(vcs, REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE])
        assert vcs.scans == 2


class TestItNeverStandsInForARepositoryThatCannotBeRead:
    def test_an_unreadable_repository_is_unknown_even_if_it_was_scanned_before(self, tmp_path):
        service.LiveIndex(Counting(), REPOS, ScanCache(tmp_path)).owners_for([ORDER_LINE])

        class Down(Counting):
            def head_sha(self, repo, ref):
                if repo == "orders":
                    raise ForgeUnavailable("fake", "down")
                return super().head_sha(repo, ref)

        idx = service.LiveIndex(Down(), REPOS, ScanCache(tmp_path))
        assert idx.owners_for([ORDER_LINE]) == {ORDER_LINE: ["billing"]}
        assert idx.unreadable == ["orders"]


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    assert ScanCache.default() is None


def test_nothing_is_cached_unless_asked(tmp_path):
    vcs = Counting()
    service.LiveIndex(vcs, REPOS).owners_for([ORDER_LINE])
    service.LiveIndex(vcs, REPOS).owners_for([ORDER_LINE])
    assert vcs.scans == 4 and not list(tmp_path.iterdir())
