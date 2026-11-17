"""What confirms a link: a topic both diffs name, outside tests; names
specific enough to mean one thing; and a pull request merged into `stage` but
not into its target is still work in flight.

Real, from a re-measure: a producer's pull request was wholly on `stage`, so
the consumer's brief dropped it as having nothing of its own, and listed in
its place a pull request stacked on it whose own diff named the topic only in
a unit test. Two candidates confirmed by short shared names were noise.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from ledger import service
from ledger.adapters.vcs.base import BranchRef, ChangedFile, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.config import Config
from ledger.core.brief import Impact
from ledger.core.contracts import ContractRef, Edge, Surface
from ledger.core.effect import delta_of
from ledger.core.join import Change
from ledger.pipeline.signals import contracts_of, edges_meet

NOW = datetime.now(timezone.utc)
TOPIC = ContractRef(Surface.TOPIC, "document.uploaded")


def test_a_topic_a_unit_test_names_is_no_contract():
    d = delta_of("core", [ChangedFile("tests/Unit/SourceChangeSubscriberTest.php",
                                      "+        $this->bus->dispatch('document.uploaded');\n")])
    assert d.contracts == {}


def test_a_topic_named_without_a_direction_is_stored_as_one():
    got = contracts_of("comms", [ChangedFile("app/kinds.py", "+DOC = 'document.uploaded'\n")])
    assert "mentions:topic:document.uploaded" in got


class TestEnds:
    def test_a_mention_meets_a_side_with_a_direction(self):
        assert edges_meet({Edge.MENTIONS}, {Edge.PUBLISHES}, Surface.TOPIC)
        assert edges_meet({Edge.CONSUMES}, {Edge.MENTIONS}, Surface.TOPIC)

    def test_two_mentions_are_a_vocabulary_not_two_ends(self):
        assert not edges_meet({Edge.MENTIONS}, {Edge.MENTIONS}, Surface.TOPIC)

    def test_two_publishers_do_not(self):
        assert not edges_meet({Edge.PUBLISHES}, {Edge.PUBLISHES}, Surface.TOPIC)

    def test_a_route_only_mentioned_does_not(self):
        assert not edges_meet({Edge.MENTIONS}, {Edge.MENTIONS}, Surface.HTTP)


def test_a_rare_topic_named_here_and_read_there_is_linked():
    vcs = FakeAdapter(
        repos=[RepoRef(full_name=r, default_branch="main", host="fake") for r in ("core", "comms")],
        branches={"comms": [BranchRef(repo="comms", name="feat/doc-kinds", head_sha="d" * 40,
                                      updated_at=NOW, author="bob@x")]},
        diffs={"comms": [ChangedFile("app/kinds.py",
                                     "+    if data['event_type'] == 'document.uploaded':\n")]},
        ahead={("comms", "main", "feat/doc-kinds"): 1})
    idx = service.LiveIndex(vcs, {"core": "main", "comms": "main"}, None)
    idx.use_changes([Change(repo="comms", ref="feat/doc-kinds", tickets=(), author="bob@x",
                            updated_at=NOW, sha="d" * 40)])
    got = idx.siblings_by_contract([TOPIC], exclude_repo="core", edges={TOPIC: {Edge.MENTIONS}})
    assert [(s.change.ref, names) for s, names in got] == [("feat/doc-kinds", ["document.uploaded"])]


class TestSpecificNames:
    def test_short_names_do_not_confirm(self):
        assert not service._specific(["recipientid", "decidedat", "claimid"][2:])
        assert not service._specific(["actionid"])

    def test_two_long_or_one_very_long_do(self):
        assert service._specific(["recipientid", "decidedat"])
        assert service._specific(["documentuploadedat"])


def git(repo, *a):
    when = (NOW - timedelta(hours=2)).isoformat()
    env = {"GIT_AUTHOR_NAME": "ann", "GIT_AUTHOR_EMAIL": "ann@example.com", "GIT_COMMITTER_NAME": "ann",
           "GIT_COMMITTER_EMAIL": "ann@example.com", "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "HOME": str(repo.parent), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                          env=env).stdout


def test_a_pull_request_merged_into_stage_not_its_target_is_read_from_its_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    service._DELTAS.clear()
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "events.py").write_text("x = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "start")
    git(work, "branch", "stage")
    git(work, "checkout", "-qb", "feat/upload-events")
    (work / "events.py").write_text("x = 1\nbus.publish('document.uploaded', doc)\n")
    git(work, "commit", "-qam", "publish uploads")
    git(work, "checkout", "-q", "stage")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge upload events", "feat/upload-events")
    git(work, "checkout", "-q", "main")
    root = tmp_path / "mirrors"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--mirror", str(work), str(root / "core.git")], check=True)
    cfg = tmp_path / "ledger.yml"
    cfg.write_text(f"org: acme\nproject_keys: [SKY]\nmirror_root: {root}\n")
    cfg, vcs = Config.load(cfg), LocalGitAdapter(root)
    head = git(work, "rev-parse", "feat/upload-events").strip()

    def described(target):
        i = Impact(repo="core", ref="feat/upload-events", kind="pr", author="ann", shared_tickets=[],
                   sha=head[:12], target=target)
        items = [i]
        idle = service.describe_impacts(items, cfg, vcs, {"core": "main"},
                                        service.LiveIndex(vcs, {"core": "main"}, None),
                                        yours={}, your_files=[], contracts=[])
        return items, idle

    items, idle = described("main")
    assert not idle and items[0].where == "stage"
    assert "document.uploaded" in (items[0].effect or "")
    assert "merged to `stage`" in items[0].evidence
    # Targeting `stage` itself, it is done: nothing of its own, not listed.
    items, idle = described("stage")
    assert idle and not items


def test_a_head_already_on_the_line_is_not_read_from_a_later_merge(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("x = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "start")
    git(work, "checkout", "-qb", "stage")
    (work / "a.py").write_text("x = 2\n")
    git(work, "commit", "-qam", "on stage itself")
    git(work, "branch", "feat/ff")                       # its head is on stage's own line
    git(work, "checkout", "-qb", "feat/other", "main")
    (work / "b.py").write_text("y = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "other")
    git(work, "checkout", "-q", "stage")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge other", "feat/other")
    vcs = LocalGitAdapter(work)
    repo = vcs.list_repos()[0].full_name
    assert vcs.landing_merge(repo, "stage", "feat/ff") is None
    assert vcs.landing_merge(repo, "stage", "feat/other") is not None


def test_a_head_behind_what_was_merged_is_still_found(tmp_path):
    """The branch moved on after the pull request's head was read, and that
    later commit is what the merge took. The merge still brought the head in."""
    work = tmp_path / "w"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("x = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "start")
    git(work, "checkout", "-qb", "stage")
    git(work, "checkout", "-qb", "feat/reminders", "main")
    (work / "b.py").write_text("y = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "first")
    behind = git(work, "rev-parse", "HEAD").strip()
    (work / "b.py").write_text("y = 2\n")
    git(work, "commit", "-qam", "review fix")
    git(work, "checkout", "-q", "stage")
    (work / "c.py").write_text("z = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "on stage")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge reminders", "feat/reminders")
    merge = git(work, "rev-parse", "HEAD").strip()
    (work / "c.py").write_text("z = 2\n")
    git(work, "commit", "-qam", "later on stage")
    vcs = LocalGitAdapter(work)
    repo = vcs.list_repos()[0].full_name
    assert vcs.landing_merge(repo, "stage", behind) == merge
    assert vcs.landing_merge(repo, "stage", "feat/reminders") == merge


def test_a_squashed_branch_has_no_landing_merge(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("x = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "start")
    git(work, "checkout", "-qb", "stage")
    git(work, "checkout", "-qb", "feat/squashed", "main")
    (work / "b.py").write_text("y = 1\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "one")
    git(work, "checkout", "-q", "stage")
    git(work, "merge", "-q", "--squash", "feat/squashed")
    git(work, "commit", "-qm", "Squashed")
    vcs = LocalGitAdapter(work)
    repo = vcs.list_repos()[0].full_name
    assert vcs.landing_merge(repo, "stage", "feat/squashed") is None


class TestWhatNamesNoTopic:
    def test_attribute_key_constants(self):
        assert contracts_of("svc", [ChangedFile("app/tracing.py",
                                                '+KEY_SERVICE_NAME = "service.name"\n'
                                                '+DEPLOYMENT_ENV_KEY = "deployment.environment"\n')]) == set()

    def test_dependency_manifests(self):
        assert contracts_of("svc", [ChangedFile("poetry.lock", '+name = "acme.tools"\n'),
                                    ChangedFile("pyproject.toml", '+build-backend = "acme.build.api"\n')]) == set()

    def test_reading_by_kind_is_consuming(self):
        assert "consumes:topic:invoice.paid" in contracts_of(
            "svc", [ChangedFile("app/feed.py", "+    if row['event_type'] == 'invoice.paid':\n")])


class TestEventKindsSpelledAsTypes:
    """A publisher's enum `ORDERS_SHIPPED = "Orders.Shipped"`, a consumer's
    `if event_type == "Orders.Shipped"`: the same event, in capitals."""

    def test_a_consumer_dispatching_on_it(self):
        got = contracts_of("orch", [ChangedFile(
            "app/services/intake.py",
            '+    if header_type in ("Orders.Placed", "Orders.Shipped"):\n')])
        assert {"consumes:topic:Orders.Placed", "consumes:topic:Orders.Shipped"} <= got

    def test_a_member_of_an_events_module(self):
        got = contracts_of("shop", [ChangedFile("app/entities/order_events.py",
                                                '+    ORDERS_SHIPPED = "Orders.Shipped"\n')])
        assert "mentions:topic:Orders.Shipped" in got

    def test_the_two_meet(self):
        assert edges_meet({Edge.MENTIONS}, {Edge.CONSUMES}, Surface.TOPIC)

    def test_the_same_shape_elsewhere_is_no_topic(self):
        assert contracts_of("svc", [ChangedFile("app/log.py", '+log = getLogger("Billing.Export")\n')]) == set()
