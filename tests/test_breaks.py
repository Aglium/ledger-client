"""Removed-field detection. A false break here is the alert people learn to
skip, so the cases that must NOT fire carry as much weight as the one that must."""

from __future__ import annotations

from ledger.core.breaks import removed_fields
from ledger.core.contracts import ContractRef, Surface

TOPIC = ContractRef(Surface.TOPIC, "invoice.documents.v1")
WANTED = {TOPIC: ["documents[].recipientId", "documents[].kind"]}


def diff(body: str, path: str = "app/api/documents.py") -> dict:
    return {path: body}


class TestFinds:
    def test_a_deleted_line_naming_the_field(self):
        got = removed_fields(diff('-    "recipientId": doc.recipient_id,'), WANTED)
        assert len(got) == 1
        assert got[0].field_path == "documents[].recipientId"
        assert got[0].evidence == '"recipientId": doc.recipient_id,'
        assert got[0].path == "app/api/documents.py"

    def test_it_matches_the_leaf_of_a_json_path(self):
        """The estate stores `documents[].recipientId`; the code writes
        `recipientId`."""
        assert removed_fields(diff("-  recipientId = payload.pop('recipientId')"), WANTED)

    def test_each_field_is_reported_once(self):
        body = "\n".join(["-  recipientId: str", "-  del recipientId", "-  x = recipientId"])
        assert len(removed_fields(diff(body), WANTED)) == 1

    def test_several_files(self):
        diffs = {"a.py": '-  "recipientId": 1',
                 "b.py": "-  documents = [{'kind': d.kind}]"}     # parent corroborates `kind`
        assert {g.field_path for g in removed_fields(diffs, WANTED)} == {
            "documents[].recipientId", "documents[].kind"}


class TestDoesNotFire:
    def test_a_field_that_is_moved_is_not_removed(self):
        body = "-    recipientId = a.recipient_id\n+    recipientId = b.recipient_id"
        assert removed_fields(diff(body), WANTED) == []

    def test_an_addition_alone(self):
        assert removed_fields(diff('+    "recipientId": doc.recipient_id,'), WANTED) == []

    def test_an_unrelated_deletion(self):
        assert removed_fields(diff("-    total = compute(order)"), WANTED) == []

    def test_a_longer_identifier_that_merely_contains_the_name(self):
        """`id` must not match `identity`, and `recipientIdentity` is not
        `recipientId`."""
        wanted = {TOPIC: ["id"]}
        assert removed_fields(diff("-    identity = invoice.identity"), wanted) == []
        assert removed_fields(diff("-    recipientIdentity = x"), WANTED) == []

    def test_a_name_inside_a_longer_dotted_path_is_not_the_field(self):
        assert removed_fields(diff("-    y = x.recipientIdentity"), WANTED) == []

    def test_very_short_field_names_are_skipped_entirely(self):
        """Two-character names match everything. Silence beats noise."""
        assert removed_fields(diff("-  ok = 1"), {TOPIC: ["ok"]}) == []

    def test_nothing_wanted_means_nothing_found(self):
        assert removed_fields(diff('-    "recipientId": 1'), {}) == []


class TestRealHistoryLessons:
    """Every case here comes from scanning 400 commits of a real repo, where
    the first version of this detector produced a stream of matches that had
    nothing to do with any contract."""

    def test_prose_is_not_a_payload(self):
        """Most early hits were sentences deleted from design notes."""
        body = "-Contract §7.2 flags intake's recipientId as required"
        assert removed_fields({"docs/adr/0016-x.md": body}, WANTED) == []
        assert removed_fields({"TODOS.md": body}, WANTED) == []
        assert removed_fields({"README.rst": body}, WANTED) == []

    def test_a_generic_name_alone_is_not_a_break(self):
        """`code`, `status`, `payload` and `source` matched almost any line."""
        wanted = {TOPIC: ["errors[].code"]}
        assert removed_fields(diff("-    code = resp.status_code"), wanted) == []

    def test_a_generic_name_counts_when_the_diff_names_the_contract(self):
        wanted = {TOPIC: ["errors[].code"]}
        body = "-    publish('invoice.documents.v1', {'code': err.code})"
        assert len(removed_fields(diff(body), wanted)) == 1

    def test_a_generic_name_counts_when_the_diff_names_the_parent(self):
        wanted = {TOPIC: ["errors[].code"]}
        body = "-    errors = [{'code': e.code} for e in failures]"
        assert len(removed_fields(diff(body), wanted)) == 1

    def test_a_distinctive_name_needs_no_corroboration(self):
        assert len(removed_fields(diff("-    recipientId = x"), WANTED)) == 1

    def test_a_comment_or_docstring_is_not_a_break(self):
        for line in ['-    """Returns the recipientId, or None."""',
                     "-    # recipientId was dropped upstream",
                     "-// recipientId: legacy",
                     "-  * recipientId"]:
            assert removed_fields(diff(line), WANTED) == [], line

    def test_a_loop_variable_is_not_a_field(self):
        """`for invoice_id in invoice_ids:` matched over and over on real history."""
        wanted = {TOPIC: ["invoice_id"]}
        assert removed_fields(diff("-    for invoice_id in invoice_ids:"), wanted) == []

    def test_the_shapes_that_ARE_field_references(self):
        for line, ok in [('-    "recipientId": doc.id,', True),
                         ("-    doc.recipientId = None", True),   # attribute: the commonest shape
                         ("-    x = doc.recipientId", True),
                         ("-    recipientId = payload['recipientId']", True),
                         ("-    recipientId, total = row", False)]:
            assert bool(removed_fields(diff(line), WANTED)) is ok, line


class TestFileMustBeAboutTheContract:
    """Without this, matching identifiers flagged SQL rewrites about other
    tables, PHP property assignments and logger refactors -- measured over
    1,200 real commits."""

    def test_a_file_that_never_names_the_contract_is_skipped(self):
        diffs = diff('-    "recipientId": doc.id')
        assert removed_fields(diffs, WANTED, file_text={"app/api/documents.py": "unrelated code"}) == []

    def test_a_file_that_names_the_contract_counts(self):
        text = {"app/api/documents.py": "publish('invoice.documents.v1', payload)"}
        assert len(removed_fields(diff('-    "recipientId": doc.id'), WANTED, file_text=text)) == 1

    def test_the_contract_named_in_the_diff_itself_counts(self):
        body = "-    publish('invoice.documents.v1', {'recipientId': x})"
        assert len(removed_fields(diff(body), WANTED, file_text={"app/api/documents.py": ""})) == 1

    def test_a_contract_named_like_an_ordinary_word_is_not_matched_this_way(self):
        """`table invoice` matched a Reviews refactor: every file says invoice."""
        plain = ContractRef(Surface.TABLE, "invoice")
        text = {"a.php": "class InvoiceService { public $invoice; }"}
        assert removed_fields({"a.php": "-    $this->state = null;"},
                              {plain: ["state"]}, file_text=text) == []

    def test_a_generic_name_assigned_to_a_local_is_not_a_break(self):
        """The last false break on 1,200 real commits:
        `code = r.metadata.get("document_type_code")`."""
        wanted = {ContractRef(Surface.TABLE, "document_type"): ["code"]}
        text = {"a.py": "document_type lookup"}
        assert removed_fields({"a.py": '-    code = r.metadata.get("document_type_code")'},
                              wanted, file_text=text) == []

    def test_but_a_generic_name_as_a_real_key_still_counts(self):
        """Corroboration is line-local on purpose: the same line must name the
        contract, not merely the same file."""
        wanted = {ContractRef(Surface.TABLE, "document_type"): ["code"]}
        text = {"a.py": "document_type lookup"}
        assert len(removed_fields({"a.py": '-    document_type["code"] = dt.code'}, wanted,
                                  file_text=text)) == 1


class TestHowGenericIsGeneric:
    """`invoice_id` matched SQL rewrites about other tables. But treating every
    `*_id` as generic silenced `documents[].matched_recipient_id` -- the exact
    field this product was built to protect."""

    TOPIC = ContractRef(Surface.TOPIC, "apex.document.results.v1")

    def test_a_specific_name_ending_in_id_is_trusted(self):
        line = "-    matched_recipient_id: UUID | None = None"
        text = {"app/entities/apex.py": "apex.document.results.v1"}
        got = removed_fields({"app/entities/apex.py": line},
                             {self.TOPIC: ["documents[].matched_recipient_id"]}, file_text=text)
        assert len(got) == 1 and got[0].field_path == "documents[].matched_recipient_id"

    def test_a_bare_foreign_key_still_needs_corroboration(self):
        line = "-    for invoice_id in invoice_ids:"
        text = {"a.py": "apex.document.results.v1"}
        assert removed_fields({"a.py": line}, {self.TOPIC: ["invoice_id"]}, file_text=text) == []

    def test_camel_case_counts_words_too(self):
        assert removed_fields(diff("-    matchedRecipientId: str"),
                              {TOPIC: ["documents[].matchedRecipientId"]},
                              file_text={"app/api/documents.py": "invoice.documents.v1"})


class TestRemovedContracts:
    from ledger.core.breaks import removed_contracts as _fn

    ENDPOINT = ContractRef(Surface.HTTP, "/api/v1/conversations")
    OWNED = {ENDPOINT: ["apex-comms"]}

    def test_a_deleted_route_that_another_repo_calls(self):
        from ledger.core.breaks import removed_contracts
        got = removed_contracts({"web.py": '-@app.get("/api/v1/conversations")'},
                                self.OWNED, {"web.py": "other routes only"})
        assert len(got) == 1 and got[0].contract == self.ENDPOINT

    def test_a_route_still_present_after_the_change_is_not_removed(self):
        """A refactor that moves the literal around leaves it in the file."""
        from ledger.core.breaks import removed_contracts
        assert removed_contracts({"web.py": '-@app.get("/api/v1/conversations")'},
                                 self.OWNED,
                                 {"web.py": 'ROUTE = "/api/v1/conversations"'}) == []

    def test_nobody_else_touches_it_so_it_is_this_repo_s_business(self):
        from ledger.core.breaks import removed_contracts
        assert removed_contracts({"web.py": '-@app.get("/api/v1/conversations")'},
                                 {self.ENDPOINT: []}, {"web.py": ""}) == []

    def test_a_contract_named_like_an_ordinary_word_is_skipped(self):
        from ledger.core.breaks import removed_contracts
        plain = ContractRef(Surface.TABLE, "invoice")
        assert removed_contracts({"a.py": "-  DROP TABLE invoice"}, {plain: ["other"]},
                                 {"a.py": ""}) == []

    def test_prose_does_not_count(self):
        from ledger.core.breaks import removed_contracts
        assert removed_contracts({"docs/api.md": "- GET /api/v1/conversations"},
                                 self.OWNED, {"docs/api.md": ""}) == []

    def test_a_mention_is_not_a_definition(self):
        """37 hits on real history were reverts and test refactors that merely
        stopped mentioning a table."""
        from ledger.core.breaks import removed_contracts
        table = ContractRef(Surface.TABLE, "invoice_line")
        owned = {table: ["acme-leads"]}
        assert removed_contracts({"t.py": "-    await seed.insert_invoice_line(db, invoice_id)"},
                                 owned, {"t.py": ""}) == []
        assert len(removed_contracts({"m.sql": "-DROP TABLE invoice_line;"}, owned, {"m.sql": ""})) == 1

    def test_a_squashed_migration_is_not_a_dropped_table(self):
        """Consolidating migrations deletes CREATE TABLE lines; the table stays."""
        from ledger.core.breaks import removed_contracts
        table = ContractRef(Surface.TABLE, "shipment_segment")
        assert removed_contracts({"m.sql": "-CREATE TABLE IF NOT EXISTS shipment_segment ("},
                                 {table: ["report-comparer"]}, {"m.sql": ""}) == []

    def test_a_topic_subscription_counts(self):
        from ledger.core.breaks import removed_contracts
        topic = ContractRef(Surface.TOPIC, "invoice.lifecycle.v1")
        owned = {topic: ["acme-core"]}
        assert len(removed_contracts({"bus.py": "-    subscribe('invoice.lifecycle.v1', handler)"},
                                     owned, {"bus.py": ""})) == 1
        assert removed_contracts({"bus.py": "-    log.info('invoice.lifecycle.v1 seen')"},
                                 owned, {"bus.py": ""}) == []

    def test_test_code_is_not_the_contract(self):
        """The last false hit: a deleted test-teardown `DROP TABLE` line."""
        from ledger.core.breaks import removed_contracts
        table = ContractRef(Surface.TABLE, "audit_log")
        owned = {table: ["acme-leads"]}
        line = '-    await db.pool.execute("DROP TABLE IF EXISTS audit_log CASCADE")'
        assert removed_contracts({"tests/conftest.py": line}, owned, {"tests/conftest.py": ""}) == []
        assert len(removed_contracts({"app/db.py": line}, owned, {"app/db.py": ""})) == 1

    def test_a_topic_default_renamed_in_its_definition_is_a_removal(self):
        """`_topic("KAFKA_TOPIC_X", "invoice.events.v1")` -> v2 was missed while
        the rule wanted "topic" as a separate word."""
        from ledger.core.breaks import removed_contracts
        topic = ContractRef(Surface.TOPIC, "invoice.events.v1")
        body = ('-INVOICE_EVENTS_TOPIC = _topic("KAFKA_TOPIC_INVOICE_EVENTS", "invoice.events.v1")\n'
                '+INVOICE_EVENTS_TOPIC = _topic("KAFKA_TOPIC_INVOICE_EVENTS", "invoice.events.v2")')
        got = removed_contracts({"app/topics.py": body}, {topic: ["billing"]}, {"app/topics.py": ""})
        assert len(got) == 1


class TestMovedNotRemoved:
    """The last false removal on real history: a constant moved out of two
    modules into a topics module, still bound to the same topic."""

    LINE = 'INVOICE_EVENTS_TOPIC = "invoice.events.v1"'

    def test_same_identifier_same_topic_elsewhere_is_a_move(self):
        from ledger.core.breaks import moved_binding
        assert moved_binding(self.LINE, "invoice.events.v1", lambda ident: {"invoice.events.v1"})

    def test_same_identifier_now_bound_to_another_topic_is_a_rename(self):
        from ledger.core.breaks import moved_binding
        assert not moved_binding(self.LINE, "invoice.events.v1", lambda ident: {"invoice.events.v2"})

    def test_a_removed_call_that_binds_nothing_is_never_a_move(self):
        from ledger.core.breaks import moved_binding
        assert not moved_binding("subscribe('invoice.events.v1', handler)", "invoice.events.v1",
                                 lambda ident: {"invoice.events.v1"})

    def test_the_defined_identifier_decides_not_the_env_var_it_reads(self):
        """A rename of the constant's default is a rename, even while an example
        env file still binds the env var to the old name."""
        from ledger.core.breaks import moved_binding
        line = 'INVOICE_EVENTS_TOPIC = _topic("KAFKA_TOPIC_INVOICE_EVENTS", "invoice.events.v1")'
        after = {"INVOICE_EVENTS_TOPIC": {"invoice.events.v2"}, "KAFKA_TOPIC_INVOICE_EVENTS": {"invoice.events.v1"}}
        assert not moved_binding(line, "invoice.events.v1", lambda ident: after.get(ident, set()))
