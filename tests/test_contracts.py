from ledger.core.contracts import (
    ContractRef, Edge, Surface, discover, discover_in_text, resolve_direction,
)


def refs(edges, surface=None):
    return {e.contract.name for e in edges if surface is None or e.contract.surface is surface}


class TestDirection:
    def test_publish_call_site_means_publishes(self):
        e = discover_in_text("r", "app/bus.py", "publish('order.events.v1', payload)")
        assert e[0].edge is Edge.PUBLISHES

    def test_subscribe_call_site_means_consumes(self):
        e = discover_in_text("r", "app/bus.py", "subscribe('shipment.data.updates.v1')")
        assert e[0].edge is Edge.CONSUMES

    def test_nearby_context_decides_when_the_line_does_not(self):
        text = "TOPIC = 'invoice.lifecycle.v1'\nasync def run():\n    await consumer.consume(TOPIC)\n"
        assert discover_in_text("r", "app/c.py", text)[0].edge is Edge.CONSUMES

    def test_ambiguous_stays_a_mention_rather_than_a_invoice(self):
        e = discover_in_text("r", "app/x.py", "NAME = 'order.events.v1'")
        assert e[0].edge is Edge.MENTIONS

    def test_publish_wins_over_a_round_trip_consume(self):
        edges = [
            ContractEdge for ContractEdge in []
        ]
        from ledger.core.contracts import ContractEdge as CE
        c = ContractRef(Surface.TOPIC, "t.v1")
        assert resolve_direction([CE("r", c, Edge.CONSUMES, "p"), CE("r", c, Edge.PUBLISHES, "p")]) is Edge.PUBLISHES


class TestSurfaces:
    def test_http_declarations(self):
        e = discover_in_text("r", "app/api.py", "@router.post('/orders/{id}/documents')")
        assert "/orders/{id}/documents" in refs(e, Surface.HTTP)

    def test_infra_endpoints_are_not_contracts(self):
        """Every service exposes /health. Sharing one is a framework convention,
        not coupling."""
        e = discover_in_text("r", "app/api.py",
                             "@app.get('/health')\n@app.get('/metrics')\n@app.get('/orders')")
        assert refs(e, Surface.HTTP) == {"/orders"}

    def test_tables_from_sql_orm_and_migrations(self):
        e = discover_in_text("r", "m.sql", "CREATE TABLE order_line (id int);")
        assert "order_line" in refs(e, Surface.TABLE)
        e = discover_in_text("r", "m.py", "op.add_column('invoice', sa.Column('x'))")
        assert "invoice" in refs(e, Surface.TABLE)
        e = discover_in_text("r", "models.py", "__tablename__ = 'customer'")
        assert "customer" in refs(e, Surface.TABLE)

    def test_table_names_are_case_normalised(self):
        e = discover_in_text("r", "m.sql", "ALTER TABLE Invoice ADD x;")
        assert refs(e, Surface.TABLE) == {"invoice"}


class TestNoise:
    def _tree(self):
        return ["app/cli.py", "app/engine/clock.py", "app/bus.py"]

    def test_module_paths_are_not_topics(self):
        """`app.engine.clock.now_utc` looks exactly like a topic. The repo's own
        file list settles it."""
        text = "from app.engine.clock import now_utc\nx = 'app.engine.clock.now_utc'\n"
        d = discover("r", self._tree(), lambda p: text if p == "app/bus.py" else "")
        assert "app.engine.clock.now_utc" not in refs(d.edges, Surface.TOPIC)

    def test_translation_namespaces_are_not_topics(self):
        keys = "\n".join(f"t('agreement.form.br.paragraph{i}')" for i in range(20))
        d = discover("r", ["app/i18n.py"], lambda p: keys)
        assert refs(d.edges, Surface.TOPIC) == set()

    def test_a_versioned_topic_survives_even_as_a_bare_mention(self):
        d = discover("r", ["app/config.py"], lambda p: "TOPIC = 'intake.drafts.v1'")
        assert "intake.drafts.v1" in refs(d.edges, Surface.TOPIC)

    def test_a_dotted_filename_is_not_a_topic(self):
        d = discover("r", ["app/x.py"], lambda p: "open('config.settings.yaml')")
        assert refs(d.edges, Surface.TOPIC) == set()

    def test_unreadable_files_do_not_abort_the_walk(self):
        def read(p):
            if p == "app/bad.py":
                raise RuntimeError("boom")
            return "publish('order.events.v1')"
        d = discover("r", ["app/bad.py", "app/good.py"], read)
        assert "order.events.v1" in refs(d.edges, Surface.TOPIC)

    def test_hostnames_are_not_topics(self):
        """Found on a real estate: two service hostnames came back as contracts
        shared between repos and escalated a brief."""
        text = "BASE = 'api.example.com'\nCB = 'invoices.example.eu'\nK8S = 'orders.default.svc'\n"
        e = discover_in_text("r", "app/settings.py", text)
        assert refs(e, Surface.TOPIC) == set()

    def test_a_topic_that_merely_contains_a_tld_word_survives(self):
        e = discover_in_text("r", "app/bus.py", "publish('com.orders.created', payload)")
        assert "com.orders.created" in refs(e, Surface.TOPIC)


class TestHttpFromRealUse:
    """Every case here is an answer that was wrong in a real session: an
    endpoint served by one repo and called by another came back as used by
    nobody."""

    def test_a_route_decorator_split_across_lines_is_found(self):
        src = '@router.get(\n    "/{invoice_id}/required-items/",\n    response_model=X,\n)'
        got = discover_in_text("orc", "app/api/v1/invoices/required_items.py", src)
        assert [(e.contract.name, e.edge) for e in got] == [("/{invoice_id}/required-items/", Edge.PUBLISHES)]

    def test_a_client_call_consumes(self):
        got = discover_in_text("ui", "src/api/client.ts", 'return api.get("/invoices/{id}/required-items/")')
        assert got[0].edge == Edge.CONSUMES

    def test_a_template_literal_url_is_found_and_consumes(self):
        got = discover_in_text("ui", "src/api/client.ts",
                               "return api.get(`${BASE}/invoices/${invoiceId}/required-items/`)")
        assert [(e.contract.name, e.edge) for e in got] == [("/invoices/{invoiceId}/required-items/", Edge.CONSUMES)]

    def test_a_generated_client_type_consumes(self):
        src = 'export interface paths {\n    "/invoices/{invoice_id}/required-items/": {\n'
        got = discover_in_text("ui", "src/api/generated/api.d.ts", src)
        assert got[0].edge == Edge.CONSUMES

    def test_a_spec_copy_does_not_make_a_repo_the_server(self):
        src = "paths:\n  /invoices/{invoice_id}/required-items/:\n    get: {}\n"
        got = discover_in_text("ui", "docs/api/openapi.invoices-v2.yaml", src)
        assert got[0].edge == Edge.MENTIONS

    def test_a_test_mock_does_not_make_a_repo_the_server(self):
        got = discover_in_text("ui", "src/x/DocumentsTab.test.tsx", '@app.get("/invoices/{id}/items")')
        assert all(e.edge == Edge.MENTIONS for e in got)


class TestRouteEquivalence:
    def test_placeholder_spellings_and_prefixes_agree(self):
        from ledger.core.contracts import http_equivalent as eq
        assert eq("/internal/v1/invoices/{invoice_id}/required-items/", "/invoices/{invoiceId}/required-items")
        assert eq("/{invoice_id}/required-items/", "/internal/v1/invoices/:id/required-items")

    def test_a_bare_id_route_matches_nothing_else(self):
        from ledger.core.contracts import http_equivalent as eq
        assert not eq("/{id}", "/users/{id}")
        assert not eq("/users/{id}", "/orders/{id}")


class TestATableNameIsNotAnIdentity:
    """Fifteen repositories in one estate "shared" `audit_log` and twelve
    "shared" `users` -- every one created from its own migrations, because they
    were generated from the same service template. Half the shared-table
    surface was this."""

    def test_two_repos_that_each_create_it_do_not_share_it(self):
        from ledger.core.contracts import own_table
        assert own_table({"leads": ["app/db/migrations.py"],
                          "people": ["app/db/migrations.py"],
                          "billing": ["alembic/versions/79444a891c5c_x.py"]})

    def test_one_owner_and_several_users_is_a_shared_table(self):
        from ledger.core.contracts import own_table
        assert not own_table({"acme-core": ["app/DoctrineMigrations/Version20230906100606.php"],
                              "api-gateway": ["app/models/invoice_document.py"],
                              "data-ingest": ["tests_functional/seed/01-acme.sql"]})

    def test_a_single_repo_is_never_a_collision(self):
        from ledger.core.contracts import own_table
        assert not own_table({"core": ["app/db/migrations.py"]})

    def test_a_repo_that_only_queries_it_is_not_a_definer(self):
        from ledger.core.contracts import own_table
        assert not own_table({"a": ["app/models/x.py"], "b": ["app/queries/y.sql"]})

    def test_every_migration_layout_counts(self):
        from ledger.core.contracts import own_table
        for a, b in (("migrations/001_up.sql", "alembic/versions/x.py"),
                     ("db/migrate/20240101_x.rb", "app/DoctrineMigrations/V1.php"),
                     ("app/db/migrations.py", "liquibase/changelog.xml")):
            assert own_table({"a": [a], "b": [b]}), (a, b)


class TestAKeywordIsNotATable:
    """`ALTER TABLE IF EXISTS x` was indexing a table called `if`, reported as
    shared across four repositories and printed in a real brief. Seven of 814
    table contracts were artifacts like this, and a visibly wrong one costs
    more trust than it saves work."""

    def tables(self, line):
        return sorted(e.contract.name for e in discover_in_text("r", "m.sql", line)
                      if e.contract.surface is Surface.TABLE)

    def test_the_existence_clause_is_not_the_table(self):
        assert self.tables("ALTER TABLE IF EXISTS invoice_note ADD COLUMN x int;") == ["invoice_note"]
        assert self.tables("DROP TABLE IF EXISTS late_fee_row;") == ["late_fee_row"]
        assert self.tables("CREATE TABLE IF NOT EXISTS invoice_marker (id uuid);") == ["invoice_marker"]

    def test_a_single_letter_is_a_query_alias(self):
        assert self.tables("SELECT * FROM invoice c JOIN note n ON n.id = c.id") == []
        assert self.tables("CREATE TABLE t (id int);") == []

    def test_a_real_table_is_still_found(self):
        assert self.tables("CREATE TABLE order_refund_row (id uuid);") == ["order_refund_row"]


class TestRouteUnderItsRouter:
    def test_the_missing_prefix_must_be_in_the_file_path(self):
        from ledger.core.contracts import under_router
        assert under_router("/{invoice_id}/assign/", "/invoices/{id}/assign", "app/api/v1/invoices/assign.py")
        assert not under_router("/{asset_id}/assign/", "/invoices/{id}/assign", "app/api/v1/assets/assign.py")

    def test_a_route_of_placeholders_only_never_matches(self):
        from ledger.core.contracts import under_router
        assert not under_router("/{id}", "/invoices/{id}", "app/invoices/routes.py")

    def test_fit_counts_the_route_words_its_file_names(self):
        from ledger.core.contracts import route_fit
        assert route_fit("/invoices/{id}/assign", "app/api/v1/invoices/assign.py") == 2
        assert route_fit("/invoices/{id}/assign", "app/api/agency.py") == 0

    def test_a_mount_prefix_further_out_need_not_be_in_the_file(self):
        """`/internal/v1` is set once where the application mounts its routers."""
        from ledger.core.contracts import under_router
        assert under_router("/{invoice_id}/assign/", "/internal/v1/invoices/{id}/assign/",
                            "app/api/v1/invoices/assign.py")
