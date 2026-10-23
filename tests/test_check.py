from ledger.core.check import Conclusion, decide
from ledger.core.join import Change, JoinResult, find_siblings


def c(repo, ref, tickets, **kw):
    return Change(repo=repo, ref=ref, tickets=tuple(tickets), **kw)


SUBJECT = c("intake-service", "feat/abc-321-export", ["ABC-321"])
SIBLING = c("orchestrator", "fix/abc-321-bind", ["ABC-321"])


class TestDecide:
    def test_not_indexed_is_neutral_never_success(self):
        """A green tick on an unexamined PR reads as 'checked, nothing there'."""
        v = decide(None)
        assert v.conclusion is Conclusion.NEUTRAL
        assert "not in the index" in v.summary
        assert not v.blocks_merge

    def test_partial_scan_is_neutral_even_with_findings(self):
        res = find_siblings(SUBJECT, [SIBLING], partial=True, missing_sources=("bitbucket",))
        v = decide(res)
        assert v.conclusion is Conclusion.NEUTRAL
        assert "bitbucket" in v.summary and "incomplete" in v.summary

    def test_siblings_found_is_advisory_not_failure(self):
        v = decide(find_siblings(SUBJECT, [SIBLING]))
        assert v.conclusion is Conclusion.NEUTRAL
        assert not v.blocks_merge
        assert "orchestrator" in v.summary
        assert "ABC-321" in v.title

    def test_clean_complete_scan_is_the_only_success(self):
        v = decide(find_siblings(SUBJECT, []))
        assert v.conclusion is Conclusion.SUCCESS
        assert "ABC-321" in v.summary

    def test_failure_is_unreachable_at_this_phase(self):
        """The gate is not earned until per-class precision is measured."""
        cases = [None,
                 find_siblings(SUBJECT, []),
                 find_siblings(SUBJECT, [SIBLING]),
                 find_siblings(SUBJECT, [SIBLING], partial=True, missing_sources=("github",)),
                 JoinResult(subject=SUBJECT, partial=True)]
        assert all(decide(x).conclusion is not Conclusion.FAILURE for x in cases)
        assert not any(decide(x).blocks_merge for x in cases)

    def test_empty_partial_does_not_invoice_all_clear(self):
        v = decide(JoinResult(subject=SUBJECT, partial=True, missing_sources=("bitbucket",)))
        assert v.conclusion is Conclusion.NEUTRAL


class TestTheOneWayToAFailingCheck:
    def result(self, **kw):
        me = Change(repo="orders", ref="feat/abc-1-x", tickets=("ABC-1",))
        return find_siblings(me, [me], **kw)

    def test_nothing_fails_unless_a_verdict_is_enforced(self):
        assert decide(self.result()).conclusion is Conclusion.SUCCESS
        assert decide(None).conclusion is Conclusion.NEUTRAL
        assert decide(self.result(partial=True, missing_sources=("github",))).conclusion is Conclusion.NEUTRAL

    def test_an_enforced_verdict_fails_and_says_why(self):
        v = decide(self.result(), enforced=("decision_needed", ["removes `id` from topic `x`"]))
        assert v.conclusion is Conclusion.FAILURE and v.blocks_merge
        assert "decision_needed" in v.title and "removes `id`" in v.summary
