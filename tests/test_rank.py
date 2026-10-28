"""Which items of a brief lead. The rules are measured, so each has a test."""

from ledger.core.rank import Item, items_of, rank, rank_brief, strength


def imp(link, **kw):
    return Item(key=f"impact:r:{link}", kind="impact", label=f"r branch `{link}`", link=link, **kw)


class TestStrength:
    def test_what_the_work_itself_links_leads(self):
        assert strength(imp("relation")) > strength(imp("author")) > strength(imp("diff_ticket")) \
            > strength(imp("ticket")) > 0

    def test_an_unreleased_commit_carrying_the_ticket_does_not(self):
        """3 of 32 useful, on a week of real briefs."""
        assert strength(imp("integration")) == 0

    def test_a_machine_accounts_branch_does_not(self):
        """1 of 17."""
        assert strength(imp("ticket", author="acme-ci-bot[bot]@users.noreply.github.com")) == 0

    def test_a_decision_or_contract_leads_only_with_a_direction(self):
        """Decisions 3 of 62, shared tables with no direction 0 of 20."""
        assert strength(Item(key="d", kind="decision", label="x")) == 0
        assert strength(Item(key="d", kind="decision", label="x", directed=True)) > 0
        assert strength(Item(key="c", kind="contract", label="x")) == 0

    def test_a_model_that_says_it_touches_the_diff_promotes(self):
        assert strength(imp("integration", touches=2)) > 0
        assert strength(imp("integration", touches=1)) == 0

    def test_a_break_always_leads(self):
        assert strength(Item(key="b", kind="break", label="x")) > strength(imp("relation"))


class TestRank:
    def test_capped_strongest_first(self):
        got = rank([imp("ticket"), imp("integration"), imp("author"), imp("diff_ticket"),
                    imp("relation")], cap=3)
        assert [x["link"] for x in got.lead] == ["relation", "author", "diff_ticket"]
        assert got.also["count"] == 2 and not got.nothing

    def test_weak_items_never_fill_an_empty_list(self):
        got = rank([imp("integration"), Item(key="c", kind="contract", label="t")])
        assert got.lead == [] and got.nothing and got.also["count"] == 2

    def test_every_lead_item_says_how_to_check_it(self):
        got = rank([imp("author")], yours=["order_line"])
        assert got.lead[0]["check"].startswith("how to check: read r branch `author` for `order_line`")

    def test_a_repeat_is_named_not_shown(self):
        got = rank([imp("author")], seen={"impact:r:author": "2026-10-14T08:00:00"})
        assert got.lead == [] and got.repeated == {"impact:r:author": "2026-10-14T08:00:00"}
        assert not got.nothing


class TestFromABrief:
    def test_link_reasons_are_read_from_the_payload(self):
        b = {"contracts": [{"surface": "table", "name": "user", "also_touched_by": ["a"],
                            "parties": [], "fields": []}],
             "impacts": [
                 {"repo": "leads", "ref": "sk-1", "author": "ann",
                  "how": "same author, pushed around the same time; no ticket links them"},
                 {"repo": "crm", "ref": "stage", "where": "stage", "sha": "0295f3833b5e",
                  "author": "kim", "shared_tickets": ["SK-1"],
                  "relation": "Ledger found no topic, route or table named by both diffs. That is"},
             ],
             "decisions": [{"repo": "vb", "path": "docs/d.md", "basis": "contract",
                            "why": "names `user`, a table this change touches"}],
             "not_local": ["table `twilio_template` is also touched on `stage` in this repository"]}
        r = rank_brief(b)
        # The stage commit is the one change carrying SK-1: it leads too.
        assert [x["key"] for x in r["lead"]] == ["impact:leads:sk-1", "impact:crm:0295f3833b5e"]
        assert r["also"]["by_kind"] == {"decision": 1, "contract": 1, "local": 1}


class TestAKeyIsNotEnough:
    """A ticket key links work only when the diffs meet. Measured on a week of
    labelled briefs: items linked by an epic key alone were noise in 24 of 60
    noise verdicts, and every useful ticket-linked item shared names."""

    def test_the_one_change_a_key_names_keeps_it(self):
        """One counterpart tagged with the key is a pointer, not an epic."""
        assert strength(imp("diff_ticket", shares=[], sole=True)) == 4

    def test_an_unreleased_merge_leads_on_names_or_a_sole_key(self):
        assert strength(imp("integration")) == 0
        assert strength(imp("integration", shares=[])) == 0
        assert strength(imp("integration", shares=["reminderlimitexceeded"])) == 3
        assert strength(imp("integration", shares=[], sole=True)) == 3

    def test_sole_is_counted_across_the_brief(self):
        def i(ref, t):
            return {"repo": "core", "ref": ref, "kind": "branch", "how": "",
                    "shared_tickets": [t], "shares": []}
        items = {x.label: x for x in items_of({"impacts": [i("a", "EP-1"), i("b", "EP-1"), i("c", "T-2")]})}
        assert [items[f"core branch `{r}`"].sole for r in "abc"] == [False, False, True]

    def test_a_key_with_nothing_else_in_common_is_weak(self):
        assert strength(imp("ticket", shares=[])) == 0
        assert strength(imp("diff_ticket", shares=[])) == 0

    def test_shared_names_keep_it(self):
        assert strength(imp("ticket", shares=["remindersstartedat"])) == 3
        assert strength(imp("diff_ticket", shares=["claimflags", "flagitems"])) == 4

    def test_a_key_both_branch_names_carry_keeps_it(self):
        """Two people named their branches for it: a claim, not an accident."""
        assert strength(imp("ticket", shares=[], named_both=True)) == 3

    def test_an_unread_diff_does_not_outrank_the_read_ones(self):
        """Past the read cap, an epic key is still all it has."""
        assert strength(imp("ticket", compared=True)) == 0
        assert strength(imp("ticket", compared=True, sole=True)) == 3

    def test_keys_an_author_link_carries_are_not_shared(self):
        b = {"impacts": [
            {"repo": "a", "ref": "x", "kind": "branch", "shared_tickets": ["T-1"], "shares": []},
            {"repo": "b", "ref": "y", "kind": "branch", "shared_tickets": ["T-1"], "shares": [],
             "how": "same author, pushed around the same time; no ticket links them"},
        ]}
        assert items_of(b)[0].sole

    def test_an_unread_diff_keeps_the_old_rule(self):
        """No diffs to compare (a forge without them) is not evidence of nothing."""
        assert strength(imp("ticket")) == 3

    def test_a_model_can_still_promote_it(self):
        assert strength(imp("ticket", shares=[], touches=2)) == 2

    def test_the_payload_says_which(self):
        b = {"subject": {"ref": "feat/sk-921-last-chance"},
             "impacts": [
                 {"repo": "core", "ref": "SK-909-core-a-stage-port3", "shared_tickets": ["SK-909"],
                  "shares": []},
                 {"repo": "core", "ref": "SK-909-core-b-stage-port4", "shared_tickets": ["SK-909"],
                  "shares": []},
                 {"repo": "core", "ref": "SK-921-robot-claim-limit", "shared_tickets": ["SK-921"],
                  "shares": []},
                 {"repo": "core", "ref": "SK-916-started-at", "shared_tickets": ["SK-916"],
                  "shares": ["remindersstartedat"]},
             ]}
        r = rank_brief(b)
        assert [x["key"] for x in r["lead"]] == ["impact:core:SK-921-robot-claim-limit",
                                                 "impact:core:SK-916-started-at"]
        assert "`remindersstartedat`" in r["lead"][1]["check"]


class TestContentLinks:
    def test_an_open_gate_naming_what_this_touches_leads(self):
        assert strength(Item(key="d", kind="decision", label="x", link="gate")) == 3
        assert strength(Item(key="d", kind="decision", label="x", link="path")) == 0

    def test_rare_shared_signals_lead(self):
        b = {"impacts": [{"repo": "billing", "ref": "feat/mine", "how":
                          "both change topic `a.b`, which little else touches (score 6); "
                          "no ticket links them"}]}
        assert rank_brief(b)["lead"][0]["link"] == "signal"
