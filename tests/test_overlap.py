from ledger.core.overlap import DiffFile, Kind, Signal, extract_signals, find_overlaps


def sig(kind, value):
    return Signal(kind, value)


PY_DIFF = DiffFile(
    "app/services/documents.py",
    """
+def bind_order_to_customer(doc, customer_id):
+    return doc
-def old_helper():
+class DocumentBinder:
+    pass
""",
)
SQL_DIFF = DiffFile(
    "migrations/versions/0042_add_customer_id.py",
    """
+def upgrade():
+    op.add_column('documents', sa.Column('customer_id', sa.String()))
+    op.create_table('customer_links')
""",
)
PHP_DIFF = DiffFile(
    "src/AppBundle/Repository/DataRepository.php",
    """
+    public function copyAllResolvedInputUpdates()
+    {
+        $sql = "INSERT INTO rr_resolved_input SELECT * FROM rr_resolved_input_updates";
+    }
""",
)
TS_DIFF = DiffFile(
    "src/api/contracts/liveContract.ts",
    """
+export const bindDocument = (id: string) => id;
+export function renderHistory() {}
""",
)


class TestExtractSignals:
    def test_python_defs_and_classes(self):
        s = extract_signals([PY_DIFF])
        assert sig(Kind.SYMBOL, "bind_order_to_customer") in s
        assert sig(Kind.SYMBOL, "DocumentBinder") in s
        assert sig(Kind.SYMBOL, "old_helper") in s, "a removed symbol still collides"
        assert sig(Kind.PATH, "app/services/documents.py") in s

    def test_migration_paths_and_table_ops(self):
        s = extract_signals([SQL_DIFF])
        assert sig(Kind.MIGRATION, "0042_add_customer_id.py") in s
        assert sig(Kind.TABLE, "documents") in s
        assert sig(Kind.TABLE, "customer_links") in s

    def test_php_methods_and_raw_sql_tables(self):
        s = extract_signals([PHP_DIFF])
        assert sig(Kind.SYMBOL, "copyAllResolvedInputUpdates") in s
        assert sig(Kind.TABLE, "rr_resolved_input") in s

    def test_typescript_exports(self):
        s = extract_signals([TS_DIFF])
        assert sig(Kind.SYMBOL, "bindDocument") in s
        assert sig(Kind.SYMBOL, "renderHistory") in s

    def test_tables_are_case_normalised(self):
        s = extract_signals([DiffFile("q.sql", "ALTER TABLE Invoices ADD x; alter table INVOICES add y;")])
        assert [x for x in s if x.kind is Kind.TABLE] == [sig(Kind.TABLE, "invoices")]

    def test_ignore_patterns_drop_noise(self):
        files = [DiffFile("uv.lock", "x"), DiffFile("app/a.py", "+def real(): pass")]
        s = extract_signals(files, ignore=(r"\.lock$",))
        assert sig(Kind.PATH, "uv.lock") not in s
        assert sig(Kind.SYMBOL, "real") in s

    def test_is_deterministic(self):
        a = extract_signals([PY_DIFF, SQL_DIFF])
        b = extract_signals([SQL_DIFF, PY_DIFF])
        assert a == b

    def test_short_names_are_skipped(self):
        """`def ab` collides with everything; short names are not signals."""
        s = extract_signals([DiffFile("a.py", "+def ab(): pass\n+def abcde(): pass")])
        assert sig(Kind.SYMBOL, "ab") not in s and sig(Kind.SYMBOL, "abcde") in s

    def test_empty_diff_yields_nothing(self):
        assert extract_signals([]) == set()


class TestFindOverlaps:
    SUBJ = ("intake-service", "feat/x")

    def test_finds_shared_table_across_repos(self):
        pop = {
            ("orchestrator", "fix/y"): {sig(Kind.TABLE, "documents"), sig(Kind.PATH, "z.py")},
            ("operator-ui", "feat/z"): {sig(Kind.PATH, "unrelated.ts")},
        }
        hits = find_overlaps(self.SUBJ, {sig(Kind.TABLE, "documents")}, pop)
        assert [h.repo for h in hits] == ["orchestrator"]
        assert hits[0].by_kind()[Kind.TABLE] == ["documents"]

    def test_ranks_stronger_signals_first(self):
        """A shared table means more than a shared file."""
        subject = {sig(Kind.TABLE, "invoices"), sig(Kind.PATH, "shared.py")}
        pop = {
            ("a", "weak"): {sig(Kind.PATH, "shared.py")},
            ("b", "strong"): {sig(Kind.TABLE, "invoices")},
        }
        assert [h.ref for h in find_overlaps(self.SUBJ, subject, pop)] == ["strong", "weak"]

    def test_excludes_self_and_same_repo(self):
        pop = {
            self.SUBJ: {sig(Kind.TABLE, "t")},
            ("intake-service", "other"): {sig(Kind.TABLE, "t")},
            ("orchestrator", "other"): {sig(Kind.TABLE, "t")},
        }
        hits = find_overlaps(self.SUBJ, {sig(Kind.TABLE, "t")}, pop)
        assert [h.repo for h in hits] == ["orchestrator"]

    def test_min_score_filters_weak_hits(self):
        pop = {("a", "b"): {sig(Kind.PATH, "p")}}
        assert find_overlaps(self.SUBJ, {sig(Kind.PATH, "p")}, pop, min_score=1)
        assert not find_overlaps(self.SUBJ, {sig(Kind.PATH, "p")}, pop, min_score=5)

    def test_no_shared_signal_is_no_hit(self):
        pop = {("a", "b"): {sig(Kind.TABLE, "other")}}
        assert find_overlaps(self.SUBJ, {sig(Kind.TABLE, "mine")}, pop) == []

    def test_ordering_is_deterministic(self):
        pop = {("a", "r"): {sig(Kind.TABLE, "t")}, ("b", "r"): {sig(Kind.TABLE, "t")}}
        one = [(h.repo, h.ref) for h in find_overlaps(self.SUBJ, {sig(Kind.TABLE, "t")}, pop)]
        two = [(h.repo, h.ref) for h in find_overlaps(self.SUBJ, {sig(Kind.TABLE, "t")}, dict(reversed(list(pop.items()))))]
        assert one == two


class TestSymbolNoise:
    """Regression for the first real benchmark finding: unqualified symbol
    matching linked four unrelated branches through `__init__` and `add`."""

    def test_ubiquitous_names_are_not_signals(self):
        diff = DiffFile("a.py", "\n".join(
            f"+    def {n}(self): pass" for n in
            ("__init__", "add", "get", "clear", "run", "handle", "upgrade", "setUp")
        ))
        assert [s for s in extract_signals([diff]) if s.kind is Kind.SYMBOL] == []

    def test_domain_names_survive(self):
        diff = DiffFile("a.py", "+def bind_order_to_customer(): pass\n+def copyAllResolvedInputUpdates(): pass")
        names = {s.value for s in extract_signals([diff]) if s.kind is Kind.SYMBOL}
        assert names == {"bind_order_to_customer", "copyAllResolvedInputUpdates"}

    def test_three_char_names_are_dropped(self):
        assert [s for s in extract_signals([DiffFile("a.py", "+def abc(): pass")]) if s.kind is Kind.SYMBOL] == []

    def test_tables_and_migrations_are_unaffected_by_the_stoplist(self):
        s = extract_signals([DiffFile("migrations/0001_x.py", "+    op.create_table('invoices')")])
        assert sig(Kind.TABLE, "invoices") in s and sig(Kind.MIGRATION, "0001_x.py") in s


class TestProseIsNotATable:
    """`UPDATE <name>` and `INSERT INTO <name>` are matched because a diff
    often only writes to a table. They match English too: at "rare shared
    signal" thresholds the surviving cross-repo links included
    `table:to` (from "update to version 2") and `table:or` (from "UPDATE or
    INSERT"), between unrelated repositories. The rejection is shared with the
    contract extractor rather than copied, so the two cannot drift.
    """

    def tables(self, body):
        return sorted(s.value for s in extract_signals([DiffFile(path="m.sql", body=body)])
                      if s.kind is Kind.TABLE)

    def test_prose_after_update_is_not_a_table(self):
        assert self.tables("update to version 2 of the sdk") == []
        assert self.tables("UPDATE or INSERT INTO invoice_note") == ["invoice_note"]
        assert self.tables("UPDATE the config and move on") == []

    def test_a_write_is_still_a_signal(self):
        assert self.tables("UPDATE invoice_call SET x = 1") == ["invoice_call"]
        assert self.tables("INSERT INTO order_refund_row VALUES (1)") == ["order_refund_row"]

    def test_the_existence_clause_is_not_the_table(self):
        assert self.tables("ALTER TABLE IF EXISTS invoice_note ADD COLUMN x int") == ["invoice_note"]
        assert self.tables("DROP TABLE IF EXISTS late_fee_row") == ["late_fee_row"]

    def test_a_trigger_row_reference_is_not_a_table(self):
        assert self.tables("UPDATE new SET x = old.x") == []
