from datetime import date

from ledger.core.decision import (
    DecisionSource, DecisionStatus, dedupe, global_id_for, parse_adr, parse_todos,
)

ADR = """---
schema_version: 1
id: "0008"
title: Handoff-to-billing — add invoiceant address to the Order.Created contract
ticket: OPS-26
status: accepted
date: 2026-07-03
deciders:
  - Alice Example
  - intake team
related_design: ../../app/services/orchestrator/invoice_creation_handler.py
---
# ADR 0008

- **Status:** accepted
The remaining gates are the inbound half (intake must populate `OrderDraft.Submitted`).
The inbound `OrderDraft.Submitted` still lacks these fields.
Everything else here is settled and needs no further work.
"""

TODOS = """# TODOS

## Admin UI

- **AI-vs-human field-provenance backend contract** (blocks Phase A.2 edit UI)
  This entry blocks the Phase A.2 editable invoice-detail UI until the backend exposes provenance.
  - **What:** an endpoint exposing normalized per-field provenance.
  - **Why:** the wizard diff component cannot be built today.
  - **Where to start:** the AI result JSON on document rows.

- **Operator directory and the assignment key** — **DECIDED 2026-09-15: keep both as they are.**
  - **What:** two costs, both accepted.
"""


class TestParseAdr:
    def test_reads_structured_frontmatter(self):
        d = parse_adr("orchestrator", "docs/adr/0008-x.md", ADR)
        assert d.status is DecisionStatus.ACCEPTED and d.is_binding
        assert d.local_ref == "0008" and d.ticket == "OPS-26"
        assert d.deciders == ["Alice Example", "intake team"]
        assert d.when == date(2026, 7, 3)

    def test_finds_open_gates_inside_an_accepted_decision(self):
        """'accepted' does not mean 'closed'. A change touching an open gate is
        exactly when someone with authority should be asked."""
        d = parse_adr("r", "docs/adr/0008-x.md", ADR)
        joined = " ".join(d.open_gates).lower()
        assert "remaining gate" in joined and "still lacks" in joined
        assert not any("settled and needs no further work" in g for g in d.open_gates)

    def test_falls_back_to_headings_without_frontmatter(self):
        d = parse_adr("r", "docs/adr/0002-y.md", "# ADR 2 — A thing\n\n- **Status:** proposed\n")
        assert d.title == "ADR 2 — A thing" and d.status is DecisionStatus.PROPOSED
        assert d.local_ref == "0002"

    def test_unknown_status_is_not_binding(self):
        d = parse_adr("r", "docs/adr/0003-z.md", "# Z\n\nno status anywhere\n")
        assert d.status is DecisionStatus.UNKNOWN and not d.is_binding

    def test_empty_file_yields_nothing(self):
        assert parse_adr("r", "p.md", "   ") is None

    def test_malformed_frontmatter_does_not_crash(self):
        d = parse_adr("r", "docs/adr/0004-a.md", "---\n: : not: yaml: [\n---\n# Title here\n")
        assert d is not None and d.title == "Title here"


class TestGlobalId:
    def test_identical_content_collapses_to_one_id(self):
        """A worktree or a copied file must not look like a second decision."""
        assert global_id_for("T", "body") == global_id_for("T", "body")

    def test_whitespace_differences_do_not_fork_it(self):
        assert global_id_for("T", "a  b\n c") == global_id_for("T", "a b c")

    def test_genuinely_different_bodies_stay_separate(self):
        assert global_id_for("T", "one") != global_id_for("T", "two")

    def test_dedupe_groups_copies_and_keeps_every_location(self):
        a = parse_adr("repo-a", "docs/adr/0008-x.md", ADR)
        b = parse_adr("repo-b", "docs/adr/0008-x.md", ADR)
        grouped = dedupe([a, b])
        assert len(grouped) == 1
        assert sorted(d.repo for d in next(iter(grouped.values()))) == ["repo-a", "repo-b"]


class TestParseTodos:
    def test_reads_top_level_entries_only(self):
        """An entry's own **What:** / **Where to start:** are fields, not decisions."""
        ds = parse_todos("orchestrator", "TODOS.md", TODOS)
        titles = [d.title for d in ds]
        assert "AI-vs-human field-provenance backend contract" in titles[0]
        assert not any(t.lower().startswith(("what", "why", "where to start")) for t in titles)
        assert len(ds) == 2

    def test_captures_the_cross_repo_blocker(self):
        ds = parse_todos("orchestrator", "TODOS.md", TODOS)
        assert any("Phase A.2" in " ".join(d.open_gates) for d in ds)

    def test_an_inline_DECIDED_marker_promotes_status(self):
        ds = parse_todos("r", "TODOS.md", TODOS)
        decided = [d for d in ds if d.status is DecisionStatus.ACCEPTED]
        assert decided and decided[0].when == date(2026, 9, 15)

    def test_source_is_marked(self):
        assert all(d.source is DecisionSource.TODO for d in parse_todos("r", "TODOS.md", TODOS))

    def test_empty_file(self):
        assert parse_todos("r", "TODOS.md", "# TODOS\n\nnothing yet\n") == []
