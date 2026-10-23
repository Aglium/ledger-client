from datetime import datetime, timedelta, timezone

from ledger.adapters.vcs.base import BranchRef, CommitRef, ForgeUnavailable, PullRef, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.core.tickets import validate_keys
from ledger.pipeline.collect import collect

KEYS = validate_keys(["ABC", "XYZ"])
NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
R = "example-org/intake-service"


def adapter(**kw):
    return FakeAdapter(
        repos=[RepoRef(full_name=R, default_branch="main", host="github.com")],
        branches={R: [
            BranchRef(repo=R, name="feat/abc-321-export", head_sha="a" * 40, updated_at=NOW),
            BranchRef(repo=R, name="feat/config-menu", head_sha="b" * 40, updated_at=NOW),
            BranchRef(repo=R, name="feat/abc-999-ancient", head_sha="c" * 40, updated_at=NOW - timedelta(days=400)),
            BranchRef(repo=R, name="main", head_sha="d" * 40, updated_at=NOW),
        ]},
        pulls={R: [PullRef(repo=R, number=12, title="ABC-234 co-customer phone",
                           head_ref="feat/abc-234-co-customer-phone", base_ref="main",
                           head_sha="e" * 40, author="alice", updated_at=NOW)]},
        **kw,
    )


class TestCollect:
    def test_collects_prs_and_ticketed_branches(self):
        got = collect([adapter()], KEYS, now=NOW)
        refs = {c.ref for c in got.changes}
        assert "feat/abc-234-co-customer-phone" in refs
        assert "feat/abc-321-export" in refs
        assert got.complete if hasattr(got, "complete") else not got.partial

    def test_drops_branches_with_no_ticket(self):
        """~700 refs must not become ~700 rows of noise."""
        got = collect([adapter()], KEYS, now=NOW)
        assert "feat/config-menu" not in {c.ref for c in got.changes}

    def test_drops_the_default_branch(self):
        assert "main" not in {c.ref for c in collect([adapter()], KEYS, now=NOW).changes}

    def test_liveness_window_excludes_stale_branches(self):
        got = collect([adapter()], KEYS, liveness_days=30, now=NOW)
        assert "feat/abc-999-ancient" not in {c.ref for c in got.changes}

    def test_widening_the_window_includes_them(self):
        got = collect([adapter()], KEYS, liveness_days=500, now=NOW)
        assert "feat/abc-999-ancient" in {c.ref for c in got.changes}

    def test_pr_takes_precedence_over_its_own_branch(self):
        got = collect([adapter()], KEYS, now=NOW)
        same = [c for c in got.changes if c.ref == "feat/abc-234-co-customer-phone"]
        assert len(same) == 1 and same[0].kind == "pr"

    def test_ticket_from_pr_title_when_branch_lacks_one(self):
        a = FakeAdapter(
            repos=[RepoRef(full_name=R, default_branch="main", host="github.com")],
            pulls={R: [PullRef(repo=R, number=3, title="XYZ-8649 retire the notes",
                               head_ref="hotfix/no-key-here", base_ref="main", head_sha="f" * 40)]},
        )
        got = collect([a], KEYS, now=NOW)
        assert got.changes[0].tickets == ("XYZ-8649",)

    def test_one_forge_down_yields_partial_not_silence(self):
        """T20: return what we have, and say what is missing."""
        got = collect([adapter(), FakeAdapter(unavailable=True)], KEYS, now=NOW)
        assert got.changes, "the healthy forge's results must survive"
        assert got.partial
        assert "fake" in got.missing_sources
        assert got.errors

    def test_all_forges_down_is_partial_and_empty(self):
        got = collect([FakeAdapter(unavailable=True)], KEYS, now=NOW)
        assert got.changes == [] and got.partial


class TestTheTicketTheBranchNameDoesNotCarry:
    """Measured on the estate this was written against: 29% of live branches
    name a ticket, 74% mention one in their commit messages. Reading the
    commits took the cross-repo ticket join from 6 linked pairs to 378, and
    from 11 branches with a sibling to 64. The join did not change; its input
    did."""

    def estate(self, *, commits, branch="announce-on-crm-link"):
        from ledger.adapters.vcs.fake import FakeAdapter
        return FakeAdapter(
            repos=[RepoRef(full_name="leads", default_branch="main", host="fake")],
            branches={"leads": [BranchRef(repo="leads", name="main", head_sha="a" * 40),
                                BranchRef(repo="leads", name=branch, head_sha="b" * 40,
                                          author="rg")]},
            commits={("leads", branch): commits},
        )

    def tickets(self, vcs):
        got = collect([vcs], ("XY",))
        return {c.ref: set(c.tickets) for c in got.changes}

    def test_a_subject_links_a_branch_whose_name_does_not(self):
        vcs = self.estate(commits=[CommitRef(sha="c" * 40, subject="XY-525 announce on crm link")])
        assert self.tickets(vcs)["announce-on-crm-link"] == {"XY-525"}

    def test_a_trailer_in_the_body_links_it_too(self):
        """A key the subject omits often sits in a trailer."""
        vcs = self.estate(commits=[CommitRef(sha="c" * 40, subject="announce on crm link",
                                             body="Refs: XY-525\nCo-authored-by: someone")])
        assert self.tickets(vcs)["announce-on-crm-link"] == {"XY-525"}

    def test_the_branch_name_still_wins_when_it_has_one(self):
        vcs = self.estate(commits=[CommitRef(sha="c" * 40, subject="wip")],
                          branch="feat/xy-600-thing")
        assert self.tickets(vcs)["feat/xy-600-thing"] == {"XY-600"}

    def test_both_are_unioned(self):
        vcs = self.estate(commits=[CommitRef(sha="c" * 40, subject="also XY-525")],
                          branch="feat/xy-600-thing")
        assert self.tickets(vcs)["feat/xy-600-thing"] == {"XY-525", "XY-600"}

    def test_a_source_that_cannot_read_commits_behaves_as_before(self):
        """Every forge adapter raises here -- it would be a call per branch.
        The signal is absent, not wrong."""
        from ledger.adapters.vcs.fake import FakeAdapter

        class NoCommits(FakeAdapter):
            def commits_between(self, repo, base, head, *, limit=None):
                raise ForgeUnavailable(self.source, "cannot enumerate commits")

        vcs = NoCommits(
            repos=[RepoRef(full_name="leads", default_branch="main", host="fake")],
            branches={"leads": [BranchRef(repo="leads", name="main", head_sha="a" * 40),
                                BranchRef(repo="leads", name="no-ticket", head_sha="b" * 40,
                                          author="rg")]})
        got = collect([vcs], ("XY",))
        assert {c.ref for c in got.changes} == {"no-ticket"}
        assert next(c for c in got.changes if c.ref == "no-ticket").tickets == ()
        assert not got.partial, "an absent signal is not a partial sync"

    def test_a_branch_does_not_inherit_the_integration_branch_s_tickets(self):
        """The defect this bound exists for. A branch cut from `stage`
        contains everything unreleased in the repository: one real branch had
        501 commits and 34 ticket keys beyond the default branch, 33 of them
        other people's, and it was reported as a sibling of all of them.
        """
        from ledger.adapters.vcs.fake import FakeAdapter
        asked: list[tuple] = []

        class Recording(FakeAdapter):
            def commits_between(self, repo, base, head, *, limit=None, exclude=()):
                asked.append((base, head, exclude))
                # Only the branch's own commit survives the exclusion.
                return [CommitRef(sha="c" * 40, subject="XY-525 the real work")]

        vcs = Recording(
            repos=[RepoRef(full_name="leads", default_branch="main", host="fake")],
            branches={"leads": [BranchRef(repo="leads", name="main", head_sha="a" * 40),
                                BranchRef(repo="leads", name="feat/thing", head_sha="b" * 40,
                                          author="rg")]})
        got = collect([vcs], ("XY",), integration_branches=("stage", "develop"))
        assert ("main", "feat/thing", ("stage", "develop")) in asked
        assert set(next(c for c in got.changes if c.ref == "feat/thing").tickets) == {"XY-525"}

    def test_the_walk_is_capped(self):
        from ledger.pipeline.collect import COMMIT_SCAN
        vcs = self.estate(commits=[CommitRef(sha=f"{i:040d}", subject="no ticket here")
                                   for i in range(1, COMMIT_SCAN + 40)])
        assert vcs.commits_between("leads", "main", "announce-on-crm-link",
                                   limit=COMMIT_SCAN).__len__() == COMMIT_SCAN

    def test_a_branch_carrying_too_many_tickets_contributes_none(self):
        """A branch with twenty-three tickets is not a change under
        twenty-three tickets; it is a parallel line of development that has
        accumulated history, and joining on all of it makes it a sibling of
        everything. Of 120 real branches carrying a ticket, 89% carry six or
        fewer and nothing carries nine or ten -- the cut sits in that gap.
        """
        from ledger.pipeline.collect import MAX_COMMIT_TICKETS
        many = [CommitRef(sha=f"{i:040d}", subject=f"XY-{i} work")
                for i in range(1, MAX_COMMIT_TICKETS + 5)]
        assert self.tickets(self.estate(commits=many))["announce-on-crm-link"] == set()

    def test_just_under_the_cap_still_counts(self):
        from ledger.pipeline.collect import MAX_COMMIT_TICKETS
        some = [CommitRef(sha=f"{i:040d}", subject=f"XY-{i} work")
                for i in range(1, MAX_COMMIT_TICKETS + 1)]
        assert len(self.tickets(self.estate(commits=some))["announce-on-crm-link"]) == \
            MAX_COMMIT_TICKETS

    def test_the_branch_name_survives_the_cap(self):
        """A name is a claim somebody made about the work; a long history is
        only what the branch has been near."""
        from ledger.pipeline.collect import MAX_COMMIT_TICKETS
        many = [CommitRef(sha=f"{i:040d}", subject=f"XY-{i} work")
                for i in range(1, MAX_COMMIT_TICKETS + 5)]
        vcs = self.estate(commits=many, branch="feat/xy-600-thing")
        assert self.tickets(vcs)["feat/xy-600-thing"] == {"XY-600"}


class TestThePullRequestDescription:
    """Both forges return it in the same call that fetched everything else, so
    reading it is free."""

    def test_a_ticket_only_in_the_description_links_the_pull_request(self):
        from ledger.adapters.vcs.fake import FakeAdapter
        vcs = FakeAdapter(
            repos=[RepoRef(full_name="leads", default_branch="main", host="fake")],
            branches={"leads": [BranchRef(repo="leads", name="main", head_sha="a" * 40)]},
            pulls={"leads": [PullRef(repo="leads", number=7, title="announce on crm link",
                                     head_ref="announce", base_ref="main", head_sha="b" * 40,
                                     body="part of XY-525, producer half")]})
        got = collect([vcs], ("XY",))
        assert set(next(c for c in got.changes if c.ref == "announce").tickets) == {"XY-525"}
