from ledger.core.join import Change, JoinResult, Sibling, find_siblings
from ledger.core.render import render_siblings


def c(repo, ref, tickets, **kw):
    return Change(repo=repo, ref=ref, tickets=tuple(tickets), **kw)


SUBJECT = c("intake-service", "feat/abc-321-bind-orders-to-a-customer", ["ABC-321"], kind="pr", pr_number=12)
POP = [
    c("orchestrator", "test/abc-321-pin-the-intake-contract", ["ABC-321"],
      kind="pr", pr_number=97, author="alice", url="https://x/97"),
    c("operator-ui", "feat/abc-321-history-shows-what-changed", ["ABC-321"], author="bob"),
]


class TestRender:
    def test_silent_when_nothing_to_say(self):
        """No comment beats a 'nothing found' comment. That is how bots get muted."""
        assert render_siblings(find_siblings(SUBJECT, [])) is None

    def test_silent_when_partial_and_empty(self):
        res = JoinResult(subject=SUBJECT, partial=True, missing_sources=("bitbucket",))
        assert render_siblings(res) is None, "a degraded empty scan must not imply all-clear"

    def test_names_the_other_author_and_change(self):
        body = render_siblings(find_siblings(SUBJECT, POP))
        assert "orchestrator" in body and "operator-ui" in body
        assert "alice" in body and "bob" in body
        assert "#97" in body and "https://x/97" in body

    def test_reads_as_an_introduction_not_a_verdict(self):
        body = render_siblings(find_siblings(SUBJECT, POP)).lower()
        for verdict_word in ("blocked", "failed", "violation", "error", "must not"):
            assert verdict_word not in body

    def test_partial_scan_is_stated_loudly(self):
        res = find_siblings(SUBJECT, POP, partial=True, missing_sources=("bitbucket",))
        body = render_siblings(res)
        assert "Partial scan" in body and "bitbucket" in body

    def test_complete_scan_carries_no_caveat(self):
        assert "Partial scan" not in render_siblings(find_siblings(SUBJECT, POP))

    def test_singular_plural(self):
        one = render_siblings(find_siblings(SUBJECT, POP[:1]))
        assert "1 other change in 1 other repo." in one
        many = render_siblings(find_siblings(SUBJECT, POP))
        assert "2 other changes in 2 other repos." in many


class TestIntegrationWork:
    """Work merged to `stage` but not released is in flight, and must not be
    rendered as if it were an open branch -- it is already done."""

    SUBJ = c("intake-service", "feat/abc-234-v2", ["ABC-234"], kind="pr", pr_number=250)

    def _res(self, n=1, **kw):
        pop = [Change(repo="orchestrator", ref=f"feat/abc-234-{i}", tickets=("ABC-234",),
                      kind="integration", integration_ref="stage",
                      title=f"feat(intake): thing {i}", author="alice", **kw) for i in range(n)]
        return find_siblings(self.SUBJ, pop)

    def test_labels_the_integration_branch_not_branch(self):
        body = render_siblings(self._res())
        assert "in `stage`" in body
        assert "| branch " not in body

    def test_says_how_many_are_already_integrated(self):
        body = render_siblings(self._res(3))
        assert "3 of these are already merged to an integration branch but not released" in body

    def test_long_lists_are_capped_and_counted(self):
        """A long table gets skimmed. The point is 'go look', not an inventory."""
        body = render_siblings(self._res(14))
        assert body.count("| `orchestrator`") == 8
        assert "_6 more_" in body

    def test_open_branches_keep_the_plain_label(self):
        pop = [Change(repo="legacy-core", ref="ABC-234-x", tickets=("ABC-234",))]
        body = render_siblings(find_siblings(self.SUBJ, pop))
        assert "branch `ABC-234-x`" in body and "already merged" not in body
