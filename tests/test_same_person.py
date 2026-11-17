"""One person across their names, and two changes near by any of their moments.

Real, from a re-measure: two halves of one change, pull requests updated three
minutes apart, had heads five hours apart and were no longer paired; a fourth
link of a token chain was signed with GitHub's noreply address while the
other three carried a laptop's default email; and two Helm charts made from
one starter were paired over their template's own name.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ledger.adapters.vcs.fake import FakeAdapter
from ledger.core.contracts import discover_in_text
from ledger.core.join import Change
from ledger.core.people import People, noreply_login
from ledger.service import LiveIndex

NOW = datetime(2026, 9, 25, 5, 40, tzinfo=timezone.utc)
LAPTOP = "dev@laptop.lan"


def pr(repo, ref, *, author=LAPTOP, login="dev", head=NOW, touched=NOW, url=None):
    return Change(repo=repo, ref=ref, tickets=(), kind="pr", author=author, login=login,
                  updated_at=head, touched_at=touched, url=url)


def index(changes):
    idx = LiveIndex(FakeAdapter(repos=[]), {})
    idx.use_changes(changes)
    return idx


class TestPeople:
    def test_a_login_and_the_email_at_its_heads_are_one_person(self):
        people = People([pr("acme/web", "feat/a")])
        assert {"dev", LAPTOP} <= people.names(LAPTOP)
        assert LAPTOP in people.names(None, "dev")

    def test_noreply_is_its_login(self):
        assert noreply_login("43089117+Dev@users.noreply.github.com") == "dev"
        people = People([pr("acme/web", "feat/a")])
        assert LAPTOP in people.names("43089117+dev@users.noreply.github.com")

    def test_an_email_under_two_logins_ties_nothing(self):
        people = People([pr("acme/web", "feat/a", author="ci@shared", login="ann"),
                         pr("acme/api", "feat/b", author="ci@shared", login="bob")])
        assert "bob" not in people.names(None, "ann")


class TestOneEmailOnTwoForges:
    """A laptop's default address signed a front end's pull request on one forge
    and a service's on another, under a different login on each; the email was
    taken for a shared machine and the two halves of one chain were strangers."""

    GH, BB = "https://github.com/acme/web/pull/1", "https://bitbucket.org/acme/api/pull-requests/2"

    def test_a_login_on_each_forge_is_one_person(self):
        people = People([pr("acme/web", "feat/a", login="dev-gh", url=self.GH),
                         pr("acme/api", "feat/a", login="dev.bb", url=self.BB)])
        assert {"dev-gh", "dev.bb", LAPTOP} <= people.names(None, "dev-gh")
        assert people.same(pr("acme/web", "x", author=None, login="dev-gh"),
                           pr("acme/api", "y", author=None, login="dev.bb"))

    def test_two_logins_on_one_forge_still_tie_nothing(self):
        people = People([pr("acme/web", "feat/a", login="ann", url=self.GH),
                         pr("acme/api", "feat/b", login="bob", url="https://github.com/acme/api/pull/3"),
                         pr("acme/svc", "feat/c", login="ann.b", url=self.BB)])
        assert people.names(None, "ann") == {"ann"}

    def test_one_login_elsewhere_does_not_join_two_logins_here(self):
        people = People([pr("acme/web", "feat/a", author="a@laptop.lan", login="ann", url=self.GH),
                         pr("acme/api", "feat/b", author="a@laptop.lan", login="ci-bot", url=self.BB),
                         pr("acme/web", "feat/c", author="b@laptop.lan", login="bob", url=self.GH),
                         pr("acme/api", "feat/d", author="b@laptop.lan", login="ci-bot", url=self.BB)])
        assert not people.same(pr("acme/web", "x", author=None, login="ann"),
                               pr("acme/web", "y", author=None, login="bob"))
        assert people.names(None, "ann") == {"ann"}

    def test_an_unknown_forge_counts_on_every_forge(self):
        people = People([pr("acme/web", "feat/a", login="ann", url=self.GH),
                         pr("acme/api", "feat/b", login="bob")])
        assert people.names(None, "ann") == {"ann"}

    def test_the_chain_is_paired_across_forges(self):
        a = pr("acme/web", "feat/token-passthrough", login="dev-gh", url=self.GH)
        b = pr("acme/api", "feat/token-passthrough", author="ci@shared", login="dev.bb", url=self.BB)
        c = pr("acme/api", "fix/other", login="dev.bb", url=self.BB, head=NOW - timedelta(days=9),
               touched=NOW - timedelta(days=9))
        got = index([a, b, c]).siblings_near(author=None, login="dev-gh", at=NOW,
                                             ref="feat/token-passthrough", exclude_repo="acme/web")
        assert [(s.change.repo, s.change.ref) for s in got] == [("acme/api", "feat/token-passthrough")]


class TestNearByEitherMoment:
    def test_pull_requests_updated_minutes_apart_are_near_though_heads_are_not(self):
        heads_apart = NOW - timedelta(hours=5, minutes=23)
        got = index([pr("acme/web", "feat/visits-form", head=heads_apart,
                        touched=NOW - timedelta(minutes=3))]).siblings_near(
            author=LAPTOP, at=NOW, touched_at=NOW, login="dev", ref="feat/visits-api",
            exclude_repo="acme/api")
        assert [s.change.ref for s in got] == ["feat/visits-form"]

    def test_heads_alone_still_decide_where_there_is_no_pull_request(self):
        got = index([pr("acme/web", "feat/visits-form", head=NOW - timedelta(hours=5),
                        touched=NOW - timedelta(hours=5))]).siblings_near(
            author=LAPTOP, at=NOW, ref="feat/visits-api", exclude_repo="acme/api")
        assert got == []

    def test_the_noreply_signed_link_joins_the_chain(self):
        changes = [pr("acme/web", "feat/touch-token-passthrough"),
                   pr("acme/core", "feat/touch-token-passthrough-core"),
                   pr("acme/leads", "feat/pass-touch-token",
                      author="43089117+dev@users.noreply.github.com")]
        got = index(changes).siblings_near(author=LAPTOP, at=NOW, login="dev",
                                           ref="feat/touch-token-passthrough", exclude_repo="acme/web")
        assert {s.change.repo for s in got} == {"acme/core", "acme/leads"}

    def test_closest_first(self):
        got = index([pr("acme/a", "feat/token-x", touched=NOW - timedelta(minutes=50), head=NOW - timedelta(days=1)),
                     pr("acme/b", "feat/token-y", touched=NOW - timedelta(minutes=5), head=NOW - timedelta(days=1))]
                    ).siblings_near(author=LAPTOP, at=NOW, ref="feat/token-z", exclude_repo="acme/c")
        assert [s.change.repo for s in got] == ["acme/b", "acme/a"]


def test_a_helm_template_name_is_not_a_topic():
    text = '{{- define "app.chart" -}}\nname: {{ include "app.fullname" . }}\n{{- end }}\n'
    for path in ("charts/app/templates/_helpers.tpl", "charts/app/templates/deployment.yaml"):
        edges = discover_in_text("acme/api", path, text)
        assert not [e for e in edges if e.contract.name in ("app.chart", "app.fullname")], path


def test_new_changes_are_new_people():
    idx = index([pr("acme/web", "feat/token-a")])
    assert idx.siblings_near(author=LAPTOP, at=NOW, ref="feat/token-b", exclude_repo="x")
    idx.use_changes([pr("acme/web", "feat/token-a", author="else@x", login="else")])
    assert idx.siblings_near(author=LAPTOP, at=NOW, ref="feat/token-b", exclude_repo="x") == []
