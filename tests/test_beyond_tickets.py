"""Linking and judging work without a ticket key, from the code alone.

The labelled estate in `lab_estate.py` holds the client to its numbers; the
rest pins down each rule on its own.
"""

from __future__ import annotations

import json

from ledger import service
from ledger.adapters.vcs.base import BranchRef, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.core.contracts import ContractRef, Edge, Surface
from ledger.core.effect import Delta, opposite_moves
from ledger.pipeline.scan import SCAN_VERSION, ScanCache

import lab_estate

PLACED = ContractRef(Surface.TOPIC, "order.placed.v1")
PRICE = ContractRef(Surface.HTTP, "/v1/products/{id}/price")

TREE = {
    "orders": {"app/events.py": 'producer.send("order.placed.v1", {"order_id": 1})\n'},
    "mailer": {"app/on_order.py": '@consumer.subscribe("order.placed.v1")\n'
                                  'def on_order(payload):\n'
                                  '    send(payload["discount_code"])\n'},
    "audit": {"app/log.py": 'log("order.placed.v1")\n# discount_code is mentioned here only\n'},
    "catalog": {"app/api.py": '@router.get("/v1/products/{id}/price")\ndef price(id):\n    pass\n'},
    "storefront": {"app/p.js": "api.get(`/v1/products/${id}/price`)\n"},
    "reports": {"migrations/001.sql": "CREATE TABLE audit_log (id int);\n"},
    "billing": {"migrations/001.sql": "CREATE TABLE audit_log (id int);\n"},
    "admin": {"app/q.py": "cur.execute('ALTER TABLE audit_log ADD c int')\n"},
}


def fake(tree=TREE):
    return FakeAdapter(
        repos=[RepoRef(full_name=r, default_branch="main", host="fake") for r in tree],
        branches={r: [BranchRef(repo=r, name="main", head_sha="a" * 40)] for r in tree},
        tree={r: dict(files) for r, files in tree.items()})


def index(cache=None):
    return service.LiveIndex(fake(), {r: "main" for r in TREE}, cache)


class TestTheLabelledEstate:
    """The numbers the client is held to. Baseline, before these rules: recall
    0.56, precision 0.83, no conflict found, every finding a correlation."""

    def test_it_finds_what_matters_without_ticket_keys_and_nothing_else(self):
        got = lab_estate.measure()
        assert got["recall"] == 1.0 and got["recall_without_ticket"] == 1.0
        assert got["precision"] == 1.0 and got["false_alarms"] == 0
        assert got["conflicts_found"] == "3/3"
        assert got["strength_right"] == 1.0

    def test_the_same_author_alone_links_nothing(self):
        """Two documentation fixes by one person half an hour apart share no
        subject word. They used to be listed as a question; now they are not
        linked at all, which is what the server has done since it measured it."""
        rows = {r["ref"]: r for r in lab_estate.measure()["rows"]}
        assert rows["catalog-docs"]["links"] == [] and rows["catalog-docs"]["got"] is None


class TestDirection:
    def test_parties_say_which_way_each_repository_faces_a_topic(self):
        got = {p["repo"]: p for p in index().parties(PLACED, exclude="orders")}
        assert got["mailer"]["uses"] == "consumes"
        assert got["mailer"]["how"] == "consumes it in `app/on_order.py`"
        assert got["audit"]["uses"] == "mentions"

    def test_a_route_is_matched_as_an_endpoint_not_a_string(self):
        got = {p["repo"]: p["uses"] for p in index().parties(PRICE)}
        assert got == {"catalog": "publishes", "storefront": "consumes"}

    def test_tables_have_no_direction_to_give(self):
        assert index().parties(ContractRef(Surface.TABLE, "audit_log")) == []

    def test_the_direction_is_remembered_with_the_scan(self, tmp_path):
        cache = ScanCache(tmp_path)
        index(cache).parties(PLACED)
        again = index(ScanCache(tmp_path))
        assert again.parties(PLACED, exclude="orders")[0]["uses"] in ("consumes", "mentions")
        assert ScanCache(tmp_path).hits == 0 and again.cache.hits > 0
        raw = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert raw["version"] == SCAN_VERSION and len(raw["contracts"][0]) == 4

    def test_a_scan_from_before_direction_was_kept_is_read_again(self, tmp_path):
        cache = ScanCache(tmp_path)
        index(cache).owners_for([PLACED])
        for f in tmp_path.glob("*.json"):
            raw = json.loads(f.read_text())
            raw["version"], raw["contracts"] = "c5.d1.s1", [c[:2] for c in raw["contracts"]]
            f.write_text(json.dumps(raw))
        fresh = ScanCache(tmp_path)
        assert {p["repo"] for p in index(fresh).parties(PLACED)} >= {"mailer"}
        assert fresh.misses == len(TREE)


class TestFieldReaders:
    def test_a_consumer_that_reads_the_name_as_a_field_is_a_reader(self):
        got = index().field_readers([PLACED], {"discount_code"}, exclude="orders")
        assert [r["repo"] for r in got[PLACED]["discount_code"]] == ["mailer"]
        assert got[PLACED]["discount_code"][0]["evidence"] == "app/on_order.py"

    def test_a_comment_naming_it_is_not(self):
        got = index().field_readers([PLACED], {"discount_code"}, exclude="orders")
        assert "audit" not in [r["repo"] for r in got[PLACED]["discount_code"]]

    def test_another_publisher_is_not_a_reader(self):
        tree = {**TREE, "orders2": {"app/e.py": 'producer.send("order.placed.v1", {"discount_code": 1})\n'}}
        idx = service.LiveIndex(fake(tree), {r: "main" for r in tree})
        got = idx.field_readers([PLACED], {"discount_code"}, exclude="orders")
        assert [r["repo"] for r in got[PLACED]["discount_code"]] == ["mailer"]

    def test_nothing_is_asked_without_names(self):
        assert index().field_readers([PLACED], set()) == {}


class TestSeparateTables:
    AUDIT = ContractRef(Surface.TABLE, "audit_log")

    def test_two_repositories_that_each_create_it_have_two_tables(self):
        got = index().separate_tables({self.AUDIT: ["admin", "billing"]}, "reports", "main", [])
        assert got == {self.AUDIT: ["admin"]}

    def test_a_table_nobody_else_creates_is_shared(self):
        got = index().separate_tables({self.AUDIT: ["billing"]}, "admin", "main", [])
        assert got == {self.AUDIT: ["billing"]}


def delta(**parts):
    d = Delta()
    for k, v in parts.items():
        setattr(d, k, v)
    return d


class TestOppositeMoves:
    def test_removing_a_route_another_change_starts_calling_cannot_land_with_it(self):
        theirs = delta(removed={PRICE: {Edge.PUBLISHES}})
        mine = delta(added={ContractRef(Surface.HTTP, "/v1/products/{pid}/price"): {Edge.CONSUMES}})
        assert opposite_moves(theirs, mine) == (
            "it REMOVES route `/v1/products/{id}/price`, which this change starts using")

    def test_it_is_seen_from_either_side(self):
        mine = delta(removed={PLACED: {Edge.PUBLISHES}})
        theirs = delta(changed={PLACED: {Edge.CONSUMES}})
        assert opposite_moves(theirs, mine) == (
            "this change REMOVES topic `order.placed.v1`, which it still uses")

    def test_two_consumers_or_a_mention_are_not_a_conflict(self):
        assert opposite_moves(delta(removed={PLACED: {Edge.CONSUMES}}),
                              delta(added={PLACED: {Edge.CONSUMES}})) is None
        assert opposite_moves(delta(removed={PLACED: {Edge.PUBLISHES}}),
                              delta(added={PLACED: {Edge.MENTIONS}})) is None

    def test_tables_are_left_out(self):
        t = ContractRef(Surface.TABLE, "audit_log")
        assert opposite_moves(delta(removed={t: {Edge.PUBLISHES}}),
                              delta(added={t: {Edge.PUBLISHES}})) is None
