from ledger.core.join import Change, branch_words, find_siblings, shared_words


def c(repo, ref, tickets, **kw):
    return Change(repo=repo, ref=ref, tickets=tuple(tickets), **kw)


# The real ABC-321 spread from the audit: three repos, one ticket.
POP = [
    c("intake-service", "feat/abc-321-bind-orders-to-a-customer", ["ABC-321"]),
    c("orchestrator", "test/abc-321-pin-the-intake-contract-against-the-consumer", ["ABC-321"]),
    c("operator-ui", "feat/abc-321-history-shows-what-changed", ["ABC-321"]),
    c("intake-service", "feat/abc-234-co-customer-phone", ["ABC-234"]),
    c("orchestrator", "feat/abc-234-late-signatures-on-the-intake-topic", ["ABC-234"]),
    c("operator-ui", "feat/config-menu", []),
]


class TestFindSiblings:
    def test_finds_the_real_cross_repo_spread(self):
        subject = POP[0]
        res = find_siblings(subject, POP)
        assert [s.change.repo for s in res.siblings] == ["operator-ui", "orchestrator"]
        assert all(s.shared_tickets == ("ABC-321",) for s in res.siblings)
        assert res.complete

    def test_excludes_self(self):
        res = find_siblings(POP[0], POP)
        assert all(s.change.ref != POP[0].ref for s in res.siblings)

    def test_cross_repo_only_by_default(self):
        # A same-repo sibling is already in the reviewer's PR list; the value is elsewhere.
        subject = c("intake-service", "feat/abc-321-export-attribution", ["ABC-321"])
        repos = {s.change.repo for s in find_siblings(subject, POP).siblings}
        assert "intake-service" not in repos

    def test_same_repo_included_when_asked(self):
        subject = c("intake-service", "feat/abc-321-export-attribution", ["ABC-321"])
        res = find_siblings(subject, POP, cross_repo_only=False)
        assert "intake-service" in {s.change.repo for s in res.siblings}

    def test_closed_changes_never_match(self):
        pop = [c("orchestrator", "fix/abc-321-old", ["ABC-321"], open=False)]
        assert find_siblings(POP[0], pop).siblings == []

    def test_no_tickets_means_no_siblings(self):
        assert find_siblings(POP[5], POP).siblings == []

    def test_empty_population(self):
        assert find_siblings(POP[0], []).siblings == []

    def test_partial_is_carried_through(self):
        # T20: a short list from a degraded forge must never look complete.
        res = find_siblings(POP[0], POP, partial=True, missing_sources=("bitbucket",))
        assert res.partial and not res.complete
        assert res.missing_sources == ("bitbucket",)

    def test_multi_ticket_change_reports_only_shared(self):
        subject = c("intake-service", "feat/abc-321-and-abc-234", ["ABC-321", "ABC-234"])
        res = find_siblings(subject, POP)
        by_repo_ref = {(s.change.repo, s.change.ref): s.shared_tickets for s in res.siblings}
        assert by_repo_ref[("orchestrator", "feat/abc-234-late-signatures-on-the-intake-topic")] == ("ABC-234",)
        assert by_repo_ref[("operator-ui", "feat/abc-321-history-shows-what-changed")] == ("ABC-321",)

    def test_ordering_is_deterministic(self):
        a = find_siblings(POP[0], POP).siblings
        b = find_siblings(POP[0], list(reversed(POP))).siblings
        assert [(s.change.repo, s.change.ref) for s in a] == [(s.change.repo, s.change.ref) for s in b]


class TestWhatABranchNameSaysItIsAbout:
    """The discriminator that made the author/time signal usable: 99 links to
    13. Every case is from the estate it was measured on."""

    def test_metadata_is_not_subject(self):
        assert branch_words("feat/xy-525-announce-on-crm-link") == {"announce", "link"}

    def test_a_shared_subject_word_is_evidence(self):
        assert shared_words("touch-token-passthrough", "touch-token-passthrough") == [
            "passthrough", "token", "touch"]
        assert shared_words("feat/communication-analytics-views",
                            "feat/communication-return-attribution") == ["communication"]

    def test_two_chores_in_the_same_afternoon_share_nothing(self):
        assert shared_words("fix/ci-minio-image", "feat/communication-analytics-views") == []
        assert shared_words("feat/network-policy", "feat/apex-comms-data-ingest-ro") == []

    def test_two_release_trains_share_nothing(self):
        """`stage` against `stage` was 16 of the 99."""
        assert shared_words("stage", "stage") == []
        assert shared_words("release/communication-attribution", "port/communications-main") == []

    def test_short_tokens_are_not_words(self):
        assert branch_words("fix/v2-ci-sa-key") == set()
