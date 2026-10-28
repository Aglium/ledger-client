"""What an in-flight change does, said to a reader who cannot open it.

The feedback these answer, after a review built on a bare pointer was checked
by the branch's own author: "The brief's fact was right, the model's
conclusion from it was inverted. The brief gave only a branch name and
author." What it lacked, in the reviewer's order: what the branch DOES, the
contract and field that cross the boundary, an evidence marker, and a
question.
"""

from __future__ import annotations

from dataclasses import dataclass

from ledger.core.contracts import ContractRef, Edge, Surface
from ledger.core.effect import (
    ask_line, delta_of, effect_line, evidence_line, relation_line, shared_contracts,
)


@dataclass
class F:
    path: str
    body: str


TOPIC = ContractRef(Surface.TOPIC, "intake.drafts.v1")

PRODUCER = [F("app/announce.py",
              "+    await producer.publish('intake.drafts.v1', {\n"
              "+        'invoice_id': invoice.id,\n"
              "+        'storage_object_key': doc.key,\n"
              "+    })\n"
              "-    'document_id': doc.id,\n")]


class TestDelta:
    def test_an_added_publish_is_added_and_says_which_way(self):
        d = delta_of("leads", PRODUCER)
        assert d.added == {TOPIC: {Edge.PUBLISHES}}
        assert not d.removed and not d.changed

    def test_a_removed_route_is_a_removal_not_a_mention(self):
        d = delta_of("api", [F("app/routes.py", "-@router.post('/invoices/{invoice_id}/assign')\n")])
        assert list(d.removed) == [ContractRef(Surface.HTTP, "/invoices/{invoice_id}/assign")]
        assert "REMOVES route `/invoices/{invoice_id}/assign`" in effect_line(d)

    def test_a_contract_named_on_both_sides_was_edited_not_added(self):
        d = delta_of("leads", [F("app/a.py", "-publish('intake.drafts.v1', a)\n"
                                             "+publish('intake.drafts.v1', a, key=k)\n")])
        assert list(d.changed) == [TOPIC] and not d.added and not d.removed

    def test_a_call_site_moved_between_files_is_not_a_removal(self):
        """The real case: an emit call site was removed from one step and
        added at another. That is a change in WHEN, not a contract going away."""
        d = delta_of("leads", [F("app/link.py", "-    publish('intake.drafts.v1', a)\n"),
                               F("app/core.py", "+    publish('intake.drafts.v1', a)\n")])
        assert list(d.changed) == [TOPIC] and not d.removed

    def test_fields_that_come_and_go_are_named(self):
        d = delta_of("leads", PRODUCER)
        assert d.keys_added == {"invoice_id", "storage_object_key"}
        assert d.keys_removed == {"document_id"}

    def test_a_field_that_only_moved_is_not_reported(self):
        d = delta_of("x", [F("a.py", "-    'invoice_ref': x,\n"), F("b.py", "+    'invoice_ref': x,\n")])
        assert not d.keys_added and not d.keys_removed

    def test_table_operations_are_said_in_words(self):
        d = delta_of("core", [F("migrations/versions/1_x.py",
                                "+    op.add_column('invoice_document', sa.Column('kind', sa.Text))\n"
                                "+    op.drop_table('legacy_upload')\n")])
        line = effect_line(d)
        assert "adds column `invoice_document.kind`" in line
        assert "drops table `legacy_upload`" in line

    def test_same_input_same_output(self):
        assert effect_line(delta_of("leads", PRODUCER)) == effect_line(delta_of("leads", PRODUCER))


class TestEffectLine:
    def test_it_says_what_is_added_and_what_is_removed(self):
        line = effect_line(delta_of("leads", PRODUCER))
        assert "starts publishing topic `intake.drafts.v1`" in line
        assert "removes field(s) `document_id`" in line
        assert "adds field(s) `invoice_id`, `storage_object_key`" in line

    def test_it_never_quotes_the_other_repositorys_code(self):
        """Names, directions and roles -- the standing rule."""
        line = effect_line(delta_of("leads", PRODUCER))
        assert "producer.publish" not in line and "invoice.id" not in line and "doc.key" not in line

    def test_a_diff_that_names_nothing_says_so_rather_than_nothing(self):
        line = effect_line(delta_of("core", [F("src/Lead.php", "+        $this->reenter();\n")]))
        assert "names no topic, route or table" in line and "1 file(s)" in line

    def test_a_long_list_is_cut_and_counted(self):
        body = "".join(f"+publish('invoice.event{n}.v1', x)\n" for n in range(7))
        assert "(+4 more)" in effect_line(delta_of("x", [F("a.py", body)]))


class TestRelation:
    def test_producer_to_consumer_names_the_contract_and_the_direction(self):
        theirs = delta_of("leads", PRODUCER)
        line, shared, _ = relation_line(theirs, {TOPIC: {Edge.CONSUMES}})
        assert line.startswith("it produces → you consume topic `intake.drafts.v1`")
        assert shared.contract == TOPIC

    def test_consumer_to_producer_is_the_other_way_round(self):
        theirs = delta_of("orch", [F("app/c.py", "+consumer.subscribe('intake.drafts.v1')\n")])
        line, _, _ = relation_line(theirs, {TOPIC: {Edge.PUBLISHES}})
        assert line.startswith("it consumes ← you produce topic `intake.drafts.v1`")

    def test_the_field_that_crosses_the_boundary_is_named(self):
        theirs = delta_of("leads", PRODUCER)
        line, _, fields = relation_line(theirs, {TOPIC: {Edge.CONSUMES}},
                                        your_keys={"document_id", "unrelated"})
        assert fields == ["document_id"] and "field(s) crossing: `document_id`" in line

    def test_a_field_the_index_knows_a_reader_of_crosses_too(self):
        theirs = delta_of("leads", PRODUCER)
        known = [{"repo": "leads", "direction": "writes", "field": "payload.storage_object_key",
                  "role": "where the file is"}]
        _, _, fields = relation_line(theirs, {TOPIC: {Edge.CONSUMES}}, known_fields=known)
        assert fields == ["storage_object_key"]

    def test_a_removal_on_their_side_is_said(self):
        theirs = delta_of("api", [F("app/routes.py", "-@router.post('/invoices/{invoice_id}/assign')\n")])
        yours = {ContractRef(Surface.HTTP, "/invoices/{id}/assign/"): {Edge.CONSUMES}}
        line, shared, _ = relation_line(theirs, yours)
        assert "REMOVES" in line and shared.removed

    def test_routes_match_as_endpoints_not_as_strings(self):
        theirs = delta_of("api", [F("app/r.py", "+@router.get('/invoices/{invoice_id}/required-items/')\n")])
        yours = {ContractRef(Surface.HTTP, "/invoices/:id/required-items"): {Edge.CONSUMES}}
        assert shared_contracts(theirs, yours)

    def test_nothing_shared_is_said_as_unrelated_not_left_blank(self):
        """A link by ticket or author with no contract in common is exactly
        where a reader over-reads a branch name."""
        theirs = delta_of("core", [F("src/Lead.php", "+        $this->reenter();\n")])
        line, shared, fields = relation_line(theirs, {TOPIC: {Edge.CONSUMES}})
        assert shared is None and fields == []
        assert "not a sign they are unrelated" in line and "the link given above stands" in line

    def test_a_repository_level_role_is_weaker_and_says_so(self):
        theirs = delta_of("leads", [F("app/x.py", "+    pass\n")])
        line, shared, _ = relation_line(theirs, {TOPIC: {Edge.CONSUMES}},
                                        repo_role="publishes topic `intake.drafts.v1`")
        assert shared is None and "related by repository, not shown to be by this branch" in line


class TestEvidence:
    def test_unmerged_work_says_so_with_its_commit(self):
        line = evidence_line(sha="bab262a405ffee", where=None, base="master", summarised=True)
        assert line.startswith("unmerged @bab262a405ff;")
        assert "You have not read it" in line and "commit title is not evidence" in line

    def test_merged_unreleased_work_says_where(self):
        line = evidence_line(sha="9f95e28", where="stage", base=None, summarised=True)
        assert line.startswith("merged to `stage` @9f95e28, not released")

    def test_an_unreadable_diff_is_not_dressed_up_as_a_summary(self):
        line = evidence_line(sha=None, where=None, base=None, summarised=False)
        assert "could not read its diff" in line and "only its existence is known" in line


class TestAsk:
    def test_it_asks_about_the_field_not_about_membership(self):
        theirs = delta_of("leads", PRODUCER)
        _, shared, fields = relation_line(theirs, {TOPIC: {Edge.CONSUMES}},
                                          your_keys={"document_id"})
        q = ask_line("xy-525-announce", "rg", shared, fields)
        assert "presence, format or meaning of `document_id`" in q
        assert "`intake.drafts.v1`" in q and q.endswith("Ask rg.")

    def test_with_no_shared_contract_it_asks_whether_they_are_related_at_all(self):
        assert ask_line("x", None, None, []).startswith("What does `x` change that this change reads")

    def test_a_removal_asks_about_order(self):
        theirs = delta_of("api", [F("app/routes.py", "-@router.post('/invoices/{invoice_id}/assign')\n")])
        _, shared, fields = relation_line(
            theirs, {ContractRef(Surface.HTTP, "/invoices/{id}/assign"): {Edge.CONSUMES}})
        assert "which lands first" in ask_line("drop-assign", "ann", shared, fields)


class TestWhatARealDiffTaughtIt:
    """A replay of four past reviews on real diffs. The reader did worse with
    these lines than without them, and each of these is why."""

    def test_a_topic_or_route_merely_named_is_not_reported(self):
        d = delta_of("core", [F("src/Repo.php", "+        ->select('c.id', 'c.status')\n"
                                                "+        $path = 'signature.png';\n")])
        line = effect_line(d)
        assert "c.id" not in line and "signature.png" not in line
        assert line.startswith("names no topic, route or table")

    def test_drop_table_if_exists_is_not_a_table_called_if(self):
        d = delta_of("x", [F("migrations/1.sql", "+DROP TABLE IF EXISTS legacy_upload;\n")])
        assert "drops table `legacy_upload`" in effect_line(d) and "`if`" not in effect_line(d)

    def test_field_names_alone_are_not_an_effect(self):
        d = delta_of("x", [F("a.py", "+    'block_reason': r,\n+    'channel': c,\n")])
        assert effect_line(d).startswith("names no topic, route or table")

    def test_no_shared_name_is_never_called_unrelated(self):
        line, _, _ = relation_line(delta_of("x", [F("a.py", "+ pass\n")]), {TOPIC: {Edge.CONSUMES}})
        assert "treat it as unrelated" not in line


class TestSharedNames:
    def test_names_from_shipped_code_only(self):
        from ledger.core.effect import names_in
        assert names_in("app/adapter.py", "+    started = payload['remindersStartedAt']\n") \
            == {"remindersstartedat"}
        assert names_in("tests/test_adapter.py", "+ reminders_started_at = 1\n") == set()
        assert names_in("docs/runtime.md", "+ reminders_started_at is read\n") == set()

    def test_snake_and_camel_are_one_name(self):
        from ledger.core.effect import names_in
        assert names_in("a.py", "+x = reminders_started_at\n") == names_in(
            "b.ts", "+const x = remindersStartedAt\n")

    def test_plain_and_common_names_link_nothing(self):
        from ledger.core.effect import names_in
        assert names_in("a.py", "+status = value\n+created_at = updated_at\n") == set()

    def test_keywords_stock_names_and_lock_files_link_nothing(self):
        from ledger.core.effect import names_in, shared_names
        a = names_in("m/1.sql", "+CREATE TABLE foo (id int);\n+ALTER TABLE bar ADD x;\n")
        b = names_in("m/2.sql", "+CREATE TABLE baz (id int);\n+ALTER TABLE qux ADD y;\n")
        assert shared_names(a, b) == []
        assert names_in("a.py", "+raise ValueError(x)\n") == set()
        assert names_in("package-lock.json", '+"node_modules/left-pad": {}\n') == set()
        assert names_in("a.py", "+TOPIC = ORDER_LINE_ARCHIVED\n") == {"orderlinearchived"}

    def test_one_long_name_or_two_short_ones(self):
        from ledger.core.effect import shared_names
        assert shared_names({"remindersstartedat"}, {"remindersstartedat"}) == ["remindersstartedat"]
        assert shared_names({"flagid"}, {"flagid"}) == []
        assert shared_names({"flagid", "unflag"}, {"flagid", "unflag", "x"}) == ["flagid", "unflag"]

    def test_a_delta_carries_them(self):
        d = delta_of("r", [F("app/a.py", "+claim_flags = read()\n"),
                           F("tests/test_a.py", "+fixture_only_name = 1\n")])
        assert d.names == {"claimflags"}
