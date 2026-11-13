"""A ticket only a pull request description names: a candidate, kept where the
diffs meet. Real: a producer in one repository and the consumer reading
`document.uploaded` in another were linked only by the description's ticket."""

from __future__ import annotations

from datetime import datetime, timezone

from ledger import service
from ledger.adapters.vcs.base import BranchRef, ChangedFile, PullRef, RepoRef
from ledger.adapters.vcs.fake import FakeAdapter
from ledger.config import Config

NOW = datetime.now(timezone.utc)

# Read out of a shared events table, so no topic or route names them: only
# the names both diffs use.
PRODUCER = [ChangedFile(path="app/events.py",
                        body="+    row['documentUploadedAt'] = now()\n")]
CONSUMER = [ChangedFile(path="app/cascade.py",
                        body="+    if row['documentUploadedAt'] > since:\n+        route(row)\n")]
UNRELATED = [ChangedFile(path="app/theme.css", body="+.button { color: red }\n")]


def estate(tmp_path, comms_diff, body="part of XY-531"):
    repos = ["core", "comms"]
    vcs = FakeAdapter(
        repos=[RepoRef(full_name=r, default_branch="main", host="fake") for r in repos],
        branches={"core": [BranchRef(repo="core", name="main", head_sha="a" * 40),
                           BranchRef(repo="core", name="upload-events", head_sha="b" * 40,
                                     updated_at=NOW, author="ann@x")],
                  "comms": [BranchRef(repo="comms", name="main", head_sha="c" * 40),
                            BranchRef(repo="comms", name="feature/XY-531-cascade", head_sha="d" * 40,
                                      updated_at=NOW, author="bob@x")]},
        pulls={"core": [PullRef(repo="core", number=8529, title="upload events", head_ref="upload-events",
                                base_ref="main", head_sha="b" * 40, author="ann", updated_at=NOW,
                                body=body)]},
        diffs={"core": PRODUCER, "comms": comms_diff},
        ahead={("core", "main", "upload-events"): 1, ("comms", "main", "feature/XY-531-cascade"): 1},
        tree={"core": {"app/events.py": "x"}, "comms": {"app/cascade.py": "x"}},
    )
    p = tmp_path / "ledger.yml"
    p.write_text("org: acme\nproject_keys: [XY]\n")
    return Config.load(p), vcs


def brief(cfg, vcs):
    service._DELTAS.clear()
    return service.compute_brief(cfg, [vcs], "core", "upload-events",
                                 lambda repos: service.LiveIndex(vcs, repos, None))


def test_a_cited_ticket_whose_diffs_meet_is_an_item(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    b = brief(*estate(tmp_path, CONSUMER))
    got = [i for i in b.impacts if i.repo == "comms"]
    assert got and got[0].how.startswith("cites XY-531")
    assert b.recommendation == "review_with"


def test_a_cited_ticket_whose_diffs_do_not_meet_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    b = brief(*estate(tmp_path, UNRELATED))
    assert [i for i in b.impacts if i.repo == "comms"] == []


def test_it_is_found_from_the_other_side_too(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_CACHE_DIR", "off")
    cfg, vcs = estate(tmp_path, CONSUMER)
    service._DELTAS.clear()
    b = service.compute_brief(cfg, [vcs], "comms", "feature/XY-531-cascade",
                              lambda repos: service.LiveIndex(vcs, repos, None))
    got = [i for i in b.impacts if i.repo == "core"]
    assert got and got[0].how.startswith("cites XY-531: its pull request's description")


def test_stored_contracts_keep_their_direction():
    from ledger.pipeline.signals import contract_of, contracts_of, paths_of
    got = contracts_of("comms", [ChangedFile(path="app/c.py",
                                             body="+@consumer.subscribe('document.uploaded')\n")])
    assert got == {"consumes:topic:document.uploaded"}
    edge, c = contract_of("publishes:http:/v1/x/:id")
    assert edge.value == "publishes" and c.name == "/v1/x/:id"
    assert paths_of(PRODUCER) == {"app/events.py"}
