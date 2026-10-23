from datetime import date

import pytest

from ledger.core.brief import (
    ContractBreak, FieldBreak, Recommendation, build_brief, link_decisions,
)
from ledger.core.contracts import ContractRef, Surface
from ledger.core.decision import Decision, DecisionSource, DecisionStatus
from ledger.core.join import Change, JoinResult, find_siblings

SUBJ = Change(repo="orchestrator", ref="fix/abc-321-bind-documents",
              tickets=("ABC-321",), kind="pr", pr_number=97,
              title="fix(documents): bind a document to the customer it names")
SIB = Change(repo="intake-service", ref="feat/abc-321-export", tickets=("ABC-321",), author="alice")
INTEGRATED = Change(repo="operator-ui", ref="feat/abc-321-history", tickets=("ABC-321",),
                    kind="integration", integration_ref="stage", author="bob")
ORDER_LINE = ContractRef(Surface.TABLE, "order_line")
#: Another repository reads `order_line`: a contract with a direction, which is
#: what a decision has to be about before it can stop anyone.
READ_ELSEWHERE = dict(contract_owners={ORDER_LINE: ["billing"]},
                      contract_parties={ORDER_LINE: [{"repo": "billing", "uses": "consumes",
                                                      "how": "declared"}]})


def adr(**kw):
    base = dict(global_id="d1", source=DecisionSource.ADR, repo="orchestrator",
                path="docs/adr/0008-x.md", title="Order.Created customer contract",
                status=DecisionStatus.ACCEPTED, deciders=["Alice Example"])
    base.update(kw)
    return Decision(**base)


class TestLinkDecisions:
    def test_ticket_match_is_strong(self):
        links = link_decisions(SUBJ, [adr(ticket="ABC-321")])
        assert links[0].strength == "strong" and "governs ticket" in links[0].why

    def test_related_design_file_match_is_strong(self):
        d = adr(related=["../../app/services/documents.py"])
        links = link_decisions(SUBJ, [d], changed_paths=["app/services/documents.py"])
        assert links[0].strength == "strong"
        assert "app/services/documents.py" in links[0].why

    def test_basename_alone_is_NOT_a_match(self):
        """Basename matching linked five unrelated ADRs to one change, because
        several point at the same common handler."""
        d = adr(related=["../../app/orchestrator/handler.py"])
        links = link_decisions(SUBJ, [d], changed_paths=["app/intake/handler.py"])
        assert all(l.strength != "strong" for l in links), \
            "same basename in a different directory is not the same file"

    def test_a_bare_filename_related_entry_is_not_a_STRONG_match(self):
        """A basename carries no location, so it cannot establish that a
        decision names the file this change touches."""
        links = link_decisions(SUBJ, [adr(related=["handler.py"])], changed_paths=["handler.py"])
        assert all(l.strength != "strong" for l in links)

    def test_word_overlap_with_a_gate_never_carries_the_gate(self):
        """The rule that produced a real false positive: a shared word between
        the change and a gate. It may leave a weak context link; it may never
        make the gate live for this change."""
        d = adr(open_gates=["the inbound OrderDraft.Submitted still lacks documents binding"])
        links = link_decisions(SUBJ, [d])
        assert all(l.gate_hit is None for l in links)
        assert all(l.strength != "strong" for l in links)

    def test_a_gate_naming_a_touched_contract_is_live(self):
        from ledger.core.contracts import ContractRef, Surface
        ct = ContractRef(Surface.TABLE, "order_line")
        d = adr(repo="other", title="Order lines", open_gates=["the order_line migration still lacks a backfill"])
        links = link_decisions(SUBJ, [d], contracts=[ct])
        assert links[0].strength == "strong"
        assert links[0].gate_hit and "order_line" in links[0].gate_basis

    def test_a_decision_governing_the_ticket_makes_its_gates_live(self):
        d = adr(ticket="ABC-321", open_gates=["the inbound payload still lacks these fields"])
        links = link_decisions(SUBJ, [d])
        assert links[0].gate_hit and links[0].gate_basis == "the decision governs this ticket"

    def test_unrelated_decision_is_not_linked(self):
        assert link_decisions(SUBJ, [adr(repo="other", title="Shipment data from elasticsearch")]) == []

    def test_strong_links_sort_first(self):
        # Distinct files: one document is one decision, whatever its hash.
        weak = adr(global_id="w", title="bind documents helper", path="docs/adr/0009-w.md")
        strong = adr(global_id="s", ticket="ABC-321", path="docs/adr/0010-s.md")
        assert link_decisions(SUBJ, [weak, strong])[0].decision_id == "s"


class TestRecommendation:
    def test_clean_change_proceeds(self):
        b = build_brief(find_siblings(SUBJ, []), [])
        assert b.recommendation == Recommendation.PROCEED.value
        assert "no cross-repo work" in b.reasons[0]

    def test_cross_repo_siblings_mean_review_with(self):
        b = build_brief(find_siblings(SUBJ, [SIB]), [])
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert "intake-service" in b.reasons[0]
        assert "alice" in b.actors

    def test_integration_work_is_called_out_separately(self):
        b = build_brief(find_siblings(SUBJ, [INTEGRATED]), [])
        assert any("not released" in r for r in b.reasons)
        assert b.impacts[0].where == "stage"

    def test_a_ticket_governing_gate_is_mentioned_but_does_NOT_escalate(self):
        """Ground truth from a real estate: one ticket held ten unrelated items,
        so "same ticket" says nothing about what a change touches. The gate is
        reported, hedged, and never a stop."""
        d = adr(ticket="ABC-321", open_gates=["the inbound OrderDraft.Submitted still lacks documents"])
        b = build_brief(find_siblings(SUBJ, [SIB]), [d])
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        text = " ".join(b.reasons)
        assert "unresolved condition" in text and "may not be about this change" in text
        assert "Alice Example" in b.actors

    def test_a_gate_naming_a_touched_contract_still_escalates(self):
        """The evidence that survives: the gate names something this change
        actually touches."""
        d = adr(ticket="ABC-321", open_gates=["`order_line` still lacks a backfill"])
        b = build_brief(find_siblings(SUBJ, [SIB]), [d],
                        contracts=[ContractRef(Surface.TABLE, "order_line")], **READ_ELSEWHERE)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value
        assert b.confidence == "high"

    def test_a_gate_over_a_contract_NOBODY_ELSE_USES_is_context(self):
        """A week of real briefs: ten decision_needed verdicts, all from
        decisions naming a table this change touches, three with nothing in
        them that bore on the change. A table only this repository uses has
        no other side to decide with."""
        d = adr(ticket="ABC-321", open_gates=["`order_line` still lacks a backfill"])
        b = build_brief(find_siblings(SUBJ, [SIB]), [d],
                        contracts=[ContractRef(Surface.TABLE, "order_line")])
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert "context, not a decision for this change" in " ".join(b.reasons)

    def test_a_gate_naming_a_touched_contract_escalates_at_HIGH(self):
        from ledger.core.contracts import ContractRef, Surface
        ct = ContractRef(Surface.TABLE, "order_line")
        d = adr(repo="other", open_gates=["the order_line migration still lacks a backfill"])
        b = build_brief(find_siblings(SUBJ, []), [d], contracts=[ct], own_commits=3, base="main",
                        **READ_ELSEWHERE)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value
        assert b.confidence == "high"

    def test_regression_one_shared_word_does_not_escalate(self):
        """Reproduces the real case: a rating change whose branch name says
        'invoice', and an unrelated eligibility ADR in ANOTHER repo whose gate
        also says 'invoice'. That produced decision_needed. It must not."""
        subj = Change(repo="legacy-core", ref="ABC-201-reviews-rating-per-invoice", tickets=("ABC-201",))
        eligibility = adr(repo="orchestrator", title="Eligibility is invoice-level; per-customer invoice split is deferred",
                          open_gates=["The operator-driven per-customer invoice split is deferred."])
        b = build_brief(find_siblings(subj, []), [eligibility], own_commits=2, base="master")
        assert b.recommendation != Recommendation.DECISION_NEEDED.value

    def test_a_superseded_decision_does_not_escalate(self):
        d = adr(ticket="ABC-321", status=DecisionStatus.SUPERSEDED,
                open_gates=["documents binding still lacks a home"])
        b = build_brief(find_siblings(SUBJ, [SIB]), [d])
        assert b.recommendation == Recommendation.REVIEW_WITH.value

    def test_INSUFFICIENT_DATA_never_resolves_to_proceed(self):
        """The rule that matters most. A partial index that says 'proceed'
        converts 'we do not know' into 'it is fine' at the moment of merge."""
        res = find_siblings(SUBJ, [], partial=True, missing_sources=("bitbucket",))
        b = build_brief(res, [])
        assert b.recommendation == Recommendation.INSUFFICIENT_DATA.value
        assert b.confidence == "low"
        assert "absence of impact is not evidence of absence" in b.reasons[0]

    def test_insufficient_data_outranks_even_a_gate_hit(self):
        d = adr(ticket="ABC-321", open_gates=["still lacks documents binding"])
        res = find_siblings(SUBJ, [SIB], partial=True, missing_sources=("github",))
        assert build_brief(res, [d]).recommendation == Recommendation.INSUFFICIENT_DATA.value

    def test_severity_ordering_is_defined(self):
        r = Recommendation
        assert r.PROCEED.rank < r.REVIEW_WITH.rank < r.DECISION_NEEDED.rank < r.INSUFFICIENT_DATA.rank


class TestContractMediatedLinking:
    """Linking through a contract is what makes an impact claim defensible:
    'touches order_line, and core writes it too' survives an argument."""

    def test_a_decision_naming_a_touched_contract_links_strongly(self):
        from ledger.core.contracts import ContractRef, Surface
        ct = ContractRef(Surface.TABLE, "order_line")
        d = adr(body="The order_line table is owned by legacy core.")
        links = link_decisions(SUBJ, [d], contracts=[ct])
        assert links[0].strength == "strong"
        assert "order_line" in links[0].why

    def test_a_decision_naming_an_untouched_contract_does_not(self):
        from ledger.core.contracts import ContractRef, Surface
        d = adr(repo="other", body="The vendor_group table is owned by core.")
        assert link_decisions(SUBJ, [d], contracts=[ContractRef(Surface.TABLE, "order_line")]) == []

    def test_substring_collisions_do_not_match(self):
        from ledger.core.contracts import ContractRef, Surface
        d = adr(repo="other", body="See order_lineation for details.")
        links = link_decisions(SUBJ, [d], contracts=[ContractRef(Surface.TABLE, "order_line")])
        assert all(l.strength != "strong" for l in links)

    def test_shared_contract_alone_triggers_review_with(self):
        """Even with no sibling branch, another repo touching the same table is
        a reason to look."""
        from ledger.core.contracts import ContractRef, Surface
        ct = ContractRef(Surface.TABLE, "order_line")
        b = build_brief(find_siblings(SUBJ, []), [], contracts=[ct],
                        contract_owners={ct: ["legacy-core", "orchestrator"]})
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert "order_line" in b.reasons[0] and "legacy-core" in b.reasons[0]

    def test_a_contract_only_this_repo_touches_is_not_a_reason(self):
        from ledger.core.contracts import ContractRef, Surface
        ct = ContractRef(Surface.TABLE, "local_only")
        b = build_brief(find_siblings(SUBJ, []), [], contracts=[ct],
                        contract_owners={ct: ["orchestrator"]})
        assert b.recommendation == Recommendation.PROCEED.value


class TestGateContract:
    """A CI plugin depends on this shape; it must not drift silently."""

    def test_serialises_to_a_stable_schema(self):
        d = adr(ticket="ABC-321", open_gates=["still lacks binding"])
        payload = build_brief(find_siblings(SUBJ, [SIB, INTEGRATED]), [d]).to_dict()
        assert payload["schema"] == 18
        assert set(payload) == {"schema", "subject", "recommendation", "confidence", "reasons",
                                "not_local", "actors", "index", "contracts", "field_breaks",
                                "contract_breaks", "impacts", "decisions", "ranked"}
        assert all("how" in i for i in payload["impacts"])
        assert set(payload["subject"]) == {"repo", "ref", "tickets", "resolved", "base", "own_commits",
                                          "uncommitted"}
        assert all("gate_basis" in d for d in payload["decisions"])
        assert set(payload["index"]) == {"complete", "missing_sources", "source",
                                         "live_repos", "unreadable_repos", "synced_at"}
        assert payload["recommendation"] in {r.value for r in Recommendation}

    def test_is_json_round_trippable(self):
        import json
        payload = build_brief(find_siblings(SUBJ, [SIB]), [adr(ticket="ABC-321")]).to_dict()
        assert json.loads(json.dumps(payload)) == payload

    def test_every_recommendation_carries_at_least_one_reason(self):
        for res, ds in (
            (find_siblings(SUBJ, []), []),
            (find_siblings(SUBJ, [SIB]), []),
            (find_siblings(SUBJ, [SIB]), [adr(ticket="ABC-321", open_gates=["still lacks x"])]),
            (find_siblings(SUBJ, [], partial=True, missing_sources=("x",)), []),
        ):
            assert build_brief(res, ds).reasons, "a verdict with no stated reason cannot be argued with"


class TestPlainWordContracts:
    """A table called `invoice` shares its name with ordinary prose. Matching it
    as prose linked nearly every ADR in an estate to any change touching it."""

    def _ct(self, name):
        from ledger.core.contracts import ContractRef, Surface
        return ContractRef(Surface.TABLE, name)

    def test_plain_word_in_prose_does_not_name_the_contract(self):
        d = adr(repo="other", title="Eligibility", body="Eligibility is decided per invoice, not per customer.")
        assert link_decisions(SUBJ, [d], contracts=[self._ct("invoice")]) == []

    def test_plain_word_in_backticks_does(self):
        d = adr(repo="other", title="Ownership", body="The `invoice` table is owned by the legacy core.")
        assert link_decisions(SUBJ, [d], contracts=[self._ct("invoice")])[0].strength == "strong"

    def test_plain_word_as_the_x_table_does(self):
        d = adr(repo="other", title="Ownership", body="Writes go through the invoice table only.")
        assert link_decisions(SUBJ, [d], contracts=[self._ct("invoice")])[0].strength == "strong"

    def test_distinctive_names_still_match_in_prose(self):
        d = adr(repo="other", title="Lines", body="Every order_line row carries a customer id.")
        assert link_decisions(SUBJ, [d], contracts=[self._ct("order_line")])[0].strength == "strong"


class TestSubjectResolution:
    def test_an_unresolved_change_is_INSUFFICIENT_DATA_never_proceed(self):
        """The real bug: a change nobody could find came back PROCEED/high."""
        ghost = Change(repo="no-such-repo", ref="", tickets=())
        b = build_brief(find_siblings(ghost, []), [], resolved=False)
        assert b.recommendation == Recommendation.INSUFFICIENT_DATA.value
        assert b.confidence == "low"
        assert "could not be found" in b.reasons[0]
        assert b.to_dict()["subject"]["resolved"] is False

    def test_unresolved_outranks_everything_else(self):
        d = adr(ticket="ABC-321", open_gates=["still lacks documents binding"])
        b = build_brief(find_siblings(SUBJ, [SIB]), [d], resolved=False)
        assert b.recommendation == Recommendation.INSUFFICIENT_DATA.value


class TestOwnCommits:
    def test_a_fully_integrated_change_touches_no_contracts(self):
        """A branch with zero commits beyond its base has no work of its own;
        attributing the base's contracts to it was the wrong-base bug."""
        from ledger.core.contracts import ContractRef, Surface
        ct = ContractRef(Surface.TABLE, "order_line")
        b = build_brief(find_siblings(SUBJ, []), [], contracts=[ct],
                        contract_owners={ct: ["billing"]}, base="stage", own_commits=0)
        assert b.contracts == []
        assert b.recommendation == Recommendation.PROCEED.value
        assert any("no commits beyond `stage`" in r for r in b.reasons)

    def test_base_and_own_commits_are_reported(self):
        payload = build_brief(find_siblings(SUBJ, []), [], base="stage", own_commits=4).to_dict()
        assert payload["subject"]["base"] == "stage" and payload["subject"]["own_commits"] == 4


class TestPluralisation:
    def test_one_integrated_sibling_reads_as_singular(self):
        b = build_brief(find_siblings(SUBJ, [INTEGRATED]), [])
        assert any("1 of them is merged" in r and "it will land" in r for r in b.reasons)


class TestDecisionsInFlight:
    """A decision written on a branch and never merged is invisible to anything
    that reads default branches. On a real estate the file naming every
    cross-repo blocker had never reached main, so the layer read as empty."""

    def test_a_drafted_decision_NAMING_WHAT_THIS_TOUCHES_asks_a_human(self):
        d = adr(ticket="ABC-321", ref="docs/adr-order-created", status=DecisionStatus.PROPOSED,
                title="`order_line` ownership moves to billing")
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [d],
                        contracts=[ContractRef(Surface.TABLE, "order_line")], **READ_ELSEWHERE)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value
        assert b.confidence == "medium"
        assert "sits on `docs/adr-order-created`" in " ".join(b.reasons)
        assert "Alice Example" in " ".join(b.reasons)

    def test_a_drafted_decision_sharing_ONLY_A_TICKET_does_not(self):
        """The measured false positive: three branches were stopped by an
        unmerged ADR about shipment-data transport, because a ten-item ticket
        happened to cover both. It is still named, at low confidence."""
        d = adr(ticket="ABC-321", ref="docs/adr-x", status=DecisionStatus.PROPOSED,
                title="Read shipment data from Elasticsearch directly")
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [d])
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert b.confidence == "low"
        text = " ".join(b.reasons)
        assert "filed under the same ticket" in text and "unmerged, on `docs/adr-x`" in text

    def test_it_outranks_sibling_work(self):
        drafted = adr(ticket="ABC-321", ref="docs/adr-x", status=DecisionStatus.PROPOSED,
                      title="`order_line` ownership moves to billing")
        b = build_brief(find_siblings(SUBJ, [SUBJ, SIB]), [drafted],
                        contracts=[ContractRef(Surface.TABLE, "order_line")], **READ_ELSEWHERE)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value
        assert "unmerged" in " ".join(b.reasons)

    def test_a_decision_on_the_changes_own_branch_does_not_block_it(self):
        """Writing the ADR as part of the change is the good case, not a blocker."""
        d = adr(ticket="ABC-321", ref=SUBJ.ref, status=DecisionStatus.PROPOSED,
                title="`order_line` ownership moves to billing")
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [d],
                        contracts=[ContractRef(Surface.TABLE, "order_line")])
        assert b.recommendation == Recommendation.REVIEW_WITH.value   # linked, not blocking
        assert "unmerged" not in " ".join(b.reasons)

    def test_a_merged_decision_is_not_reported_as_drafted(self):
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [adr(ticket="ABC-321")])
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert "unmerged" not in " ".join(b.reasons)

    def test_the_link_says_where_it_is_being_written(self):
        links = link_decisions(SUBJ, [adr(ticket="ABC-321", ref="docs/adr-x")])
        assert links[0].ref == "docs/adr-x" and "on `docs/adr-x`, not merged" in links[0].why

    def test_the_same_decision_on_six_branches_is_one_link(self):
        """Six copies of one ADR on six branches produced six identical links."""
        copies = [adr(ticket="ABC-321", ref=f"chore/branch-{i}") for i in range(6)]
        links = link_decisions(SUBJ, copies)
        assert len(links) == 1
        assert links[0].ref == "chore/branch-0" and "5 other branch(es)" in links[0].why

    def test_a_merged_copy_wins_over_branch_copies(self):
        links = link_decisions(SUBJ, [adr(ticket="ABC-321", ref="chore/x"), adr(ticket="ABC-321")])
        assert len(links) == 1 and links[0].ref is None
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [adr(ticket="ABC-321", ref="chore/x"),
                                                      adr(ticket="ABC-321")])
        assert b.recommendation == Recommendation.REVIEW_WITH.value


class TestFieldBreaks:
    """The canonical case: a field disappears and another service breaks. No
    spec file mentions it, no test in this repo fails."""

    def brk(self, **kw):
        base = dict(surface="topic", name="invoice.documents.v1",
                    field_path="documents[].recipientId",
                    readers=[{"repo": "orchestrator", "direction": "requires_present",
                              "role": "the recipient this document belongs to", "where": "app/x.py"}],
                    evidence='-    "recipientId": doc.recipient_id,', path="app/api/documents.py")
        base.update(kw)
        return FieldBreak(**base)

    def test_a_removed_field_someone_reads_outranks_everything_else(self):
        b = build_brief(find_siblings(SUBJ, [SUBJ, SIB]), [adr(ticket="ABC-321")],
                        field_breaks=[self.brk()], own_commits=2)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value
        assert b.confidence == "high"
        reason = " ".join(b.reasons)
        assert "removes `documents[].recipientId`" in reason
        assert "orchestrator (requires_present)" in reason
        assert "the recipient this document belongs to" in reason

    def test_it_reaches_the_gate_payload(self):
        payload = build_brief(find_siblings(SUBJ, [SUBJ]), [], field_breaks=[self.brk()],
                              own_commits=1).to_dict()
        assert payload["schema"] == 18
        fb = payload["field_breaks"][0]
        assert fb["field_path"] == "documents[].recipientId"
        assert fb["readers"][0]["repo"] == "orchestrator" and fb["evidence"].startswith("-")

    def test_an_already_integrated_change_reports_none(self):
        """No commits of its own means it removed nothing of its own."""
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [], field_breaks=[self.brk()],
                        own_commits=0, base="stage")
        assert b.field_breaks == [] and b.recommendation != Recommendation.DECISION_NEEDED.value


class TestContractBreaks:
    """Deleting the route, topic or table itself -- the larger sibling of
    deleting one of its fields."""

    def brk(self):
        return ContractBreak(surface="http", name="/api/v1/conversations",
                             also_touched_by=["apex-comms"],
                             evidence='-@app.get("/api/v1/conversations")', path="src/web.py")

    def test_it_asks_a_human_and_names_who_else_uses_it(self):
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [], contract_breaks=[self.brk()], own_commits=1)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value and b.confidence == "high"
        assert "stops naming http `/api/v1/conversations`" in " ".join(b.reasons)
        assert "apex-comms" in " ".join(b.reasons)

    def test_it_reaches_the_payload(self):
        payload = build_brief(find_siblings(SUBJ, [SUBJ]), [], contract_breaks=[self.brk()],
                              own_commits=1).to_dict()
        assert payload["contract_breaks"][0]["name"] == "/api/v1/conversations"


class TestAmendedDecisions:
    """Real case: an accepted ADR on main, edited on five branches. Content
    hashes differ, so it read as five unmerged decisions and stopped a change
    the merged ADR already governed."""

    def copies(self):
        merged = adr(global_id="doc:2f41", ticket="ABC-321", path="docs/adr/0002-x.md",
                     title="`order_line` aggregation")
        edits = [adr(global_id="doc:014d", ticket="ABC-321", path="docs/adr/0002-x.md",
                     title="`order_line` aggregation", ref=f"docs/branch-{i}") for i in range(5)]
        return [merged, *edits]

    def test_one_link_and_it_is_the_merged_copy(self):
        links = link_decisions(SUBJ, self.copies(), contracts=[ContractRef(Surface.TABLE, "order_line")])
        assert len(links) == 1 and links[0].ref is None
        assert "being amended on 5 branch(es)" in links[0].why

    def test_a_merged_decision_being_amended_does_not_stop_the_change(self):
        b = build_brief(find_siblings(SUBJ, [SUBJ]), self.copies(),
                        contracts=[ContractRef(Surface.TABLE, "order_line")])
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert "unmerged" not in " ".join(b.reasons)

    def test_different_todo_entries_in_one_file_stay_separate(self):
        a = adr(source=DecisionSource.TODO, path="TODOS.md", title="Backfill `order_line`", ticket="ABC-321")
        b = adr(source=DecisionSource.TODO, path="TODOS.md", title="Split `order_line` rows",
                global_id="d2", ticket="ABC-321")
        assert len(link_decisions(SUBJ, [a, b])) == 2


class TestDecisionsOnIntegrationBranches:
    """Real case: an accepted ADR on `stage` but not yet on main stopped a
    branch as if the decision were still being drafted. Integrated is decided."""

    def on(self, ref, **kw):
        return adr(ref=ref, path="docs/adr/0014-x.md", title="`order_line` rule hierarchy", **kw)

    def test_an_adr_on_stage_is_context_not_a_stop(self):
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [self.on("stage")],
                        contracts=[ContractRef(Surface.TABLE, "order_line")],
                        integration_branches=("stage",))
        assert b.recommendation == Recommendation.REVIEW_WITH.value
        assert b.decisions[0].integrated and "not yet on the default branch" in b.decisions[0].why

    def test_the_stage_copy_beats_topic_branch_copies(self):
        copies = [self.on("docs/a"), self.on("stage"), self.on("feat/b")]
        links = link_decisions(SUBJ, copies, contracts=[ContractRef(Surface.TABLE, "order_line")],
                               integration_branches=("stage",))
        assert len(links) == 1 and links[0].ref == "stage"

    def test_only_on_topic_branches_still_escalates(self):
        b = build_brief(find_siblings(SUBJ, [SUBJ]), [self.on("docs/a")],
                        contracts=[ContractRef(Surface.TABLE, "order_line")],
                        integration_branches=("stage",), **READ_ELSEWHERE)
        assert b.recommendation == Recommendation.DECISION_NEEDED.value


class TestMergedWorkNamesItsCommit:
    """From the first `useful` feedback the ledger ever recorded, which also
    named a gap: "the branch name it gave wasn't in the local clone. The work
    was commit 9f95e28 on stage." A branch deleted when it merged sends the
    reader looking for something that is not there."""

    def test_an_integration_impact_carries_the_commit(self):
        merged = Change(repo="leads", ref="PLT-201-rating-in-leads", tickets=("ABC-321",),
                        kind="integration", integration_ref="stage", sha="9f95e28abcdef0123456")
        b = build_brief(find_siblings(SUBJ, [merged]), []).to_dict()
        i = b["impacts"][0]
        assert i["where"] == "stage" and i["sha"] == "9f95e28abcde"

    def test_an_open_branch_has_no_commit_to_name(self):
        b = build_brief(find_siblings(SUBJ, [SIB]), []).to_dict()
        assert b["impacts"][0]["sha"] is None

    def test_the_agent_reads_both(self):
        from ledger.surfaces import hook as h
        merged = Change(repo="leads", ref="PLT-201-rating-in-leads", tickets=("ABC-321",),
                        kind="integration", integration_ref="stage", sha="9f95e28abcdef0123456")
        text = h.summarise(build_brief(find_siblings(SUBJ, [merged]), []).to_dict())
        assert "PLT-201-rating-in-leads (in stage, 9f95e28abcde)" in text
