"""Coverage says what Ledger could see; feedback keeps what people said about it, here."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from ledger import coverage as cv
from ledger import feedback as fb
from ledger import replay as rp
from ledger.adapters.vcs.localgit import LocalGitAdapter
from ledger.cli import app
from ledger.config import Config

from test_replay import SINCE, by_ref, estate, merge, repo  # noqa: F401 -- fixture


class TestCoverage:
    def test_it_names_every_repository_and_its_history(self, estate):
        cfg, root = estate
        cov = cv.survey(cfg, LocalGitAdapter(str(root)), days=7)
        got = {r.repo: r.pull_requests for r in cov.repos}
        assert got == {"billing": 1, "orders": 3}
        assert cov.verdict().startswith("ready to replay: 2 repositories readable")
        assert cov.gaps() == []

    def test_one_repository_is_not_enough_and_it_says_so(self, tmp_path):
        repo(tmp_path / "src", "orders", {"a.py": "x\n"})
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        cov = cv.survey(Config.load(str(p)), LocalGitAdapter(str(tmp_path / "src")))
        assert cov.verdict().startswith("not enough coverage")
        assert "needs at least two" in cov.gaps()[0]

    def test_no_pull_requests_is_not_enough_history(self, tmp_path):
        repo(tmp_path / "src", "orders", {"a.py": "x\n"})
        repo(tmp_path / "src", "billing", {"b.py": "x\n"})
        p = tmp_path / "l.yml"
        p.write_text("org: acme\nproject_keys: [ABC]\n")
        cov = cv.survey(Config.load(str(p)), LocalGitAdapter(str(tmp_path / "src")))
        assert cov.verdict().startswith("not enough history")

    def test_repositories_the_forge_lists_that_are_not_here_are_named(self, estate):
        from ledger.adapters.vcs.base import RepoRef

        class Forge:
            source = "github"

            def list_repos(self):
                return [RepoRef(full_name=f"acme/{n}", default_branch="main", host="github.com",
                                url=f"https://github.com/acme/{n}")
                        for n in ("orders", "billing", "shipping")]

        cfg, root = estate
        cov = cv.survey(cfg, LocalGitAdapter(str(root)), forge=[Forge()])
        assert cov.not_here == ["shipping"]
        assert any("not on this machine" in g and "shipping" in g for g in cov.gaps())

    def test_prerequisites_say_what_to_do(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        checks = {c.name: c for c in cv.prerequisites(None)}
        assert checks["python"].ok and checks["git"].ok
        assert not checks["config"].ok and "aglium-ledger init" in checks["config"].detail

    def test_the_command(self, estate, tmp_path):
        cfg, root = estate
        res = CliRunner().invoke(app, ["coverage", "--config", str(tmp_path / "ledger.yml"),
                                       "--local-root", str(root), "--days", "7"])
        assert res.exit_code == 0, res.output
        assert "REPOSITORIES LEDGER CAN SEE  (2 readable" in res.output
        assert "ready to replay" in res.output and "aglium-ledger replay --everyone" in res.output
        res = CliRunner().invoke(app, ["coverage", "--config", str(tmp_path / "ledger.yml"),
                                       "--local-root", str(root), "--json"])
        doc = json.loads(res.output)
        assert {r["repo"] for r in doc["coverage"]["repos"]} == {"billing", "orders"}


class TestFeedback:
    def test_the_four_answers_and_their_older_words(self):
        assert [fb.normalise(v) for v in ("useful", "already known", "false positive",
                                          "can't verify", "noise", "wrong", "missed")] == [
            "useful", "known", "false_positive", "insufficient_evidence", "known",
            "false_positive", "missed"]

    def test_nothing_but_names_and_a_verdict_is_kept(self):
        fb.record("useful", repo="orders", ref="feat/x", sha="a" * 40, note="caught it")
        [row] = fb.rows()
        assert set(row) <= {"at", "verdict", "repo", "ref", "sha", "recommendation", "case",
                            "event_id", "source", "note"}

    def test_a_replay_report_is_imported_once(self, estate, tmp_path):
        cfg, root = estate
        got = rp.run(cfg, LocalGitAdapter(str(root)), since=SINCE, author=None)
        md, _ = rp.write(got, tmp_path / "ledger-replay.md")
        flight = by_ref(got)["feat/abc-7-read-lines"].change.id
        table = by_ref(got)["tidy-order-lines"].change.id
        readme = by_ref(got)["feat/abc-9-readme"].change.id
        text = md.read_text()
        text = text.replace(f"(`{flight}`):  [ ] useful", f"(`{flight}`):  [x] useful")
        line = next(ln for ln in text.splitlines() if f"(`{table}`):" in ln)
        text = text.replace(line, line.replace("[ ] false positive", "[x] false positive"))
        text = text.replace(f"- [ ] missed (`{readme}`)", f"- [x] missed (`{readme}`)")
        md.write_text(text)
        kept, skipped = fb.import_report(md)
        assert sorted((k["case"], k["verdict"]) for k in kept) == sorted(
            [(flight, "useful"), (table, "false_positive"), (readme, "missed")])
        useful = next(k for k in kept if k["case"] == flight)
        assert (useful["repo"], useful["ref"], useful["recommendation"]) == (
            "orders", "feat/abc-7-read-lines", "review_with")
        assert fb.import_report(md)[0] == []

    def test_a_shared_summary_hides_the_names(self):
        fb.record("useful", repo="secret-payments", ref="feat/x", recommendation="review_with",
                  note="private words")
        fb.record("false_positive", repo="secret-payments", ref="feat/y")
        s = fb.summary(share=True)
        assert s["by_verdict"]["useful"] == 1 and s["rates"]["useful"] == 0.5
        blob = json.dumps(s)
        assert "secret-payments" not in blob and "feat/x" not in blob and "private words" not in blob
        a, b = s["answers_hashed"]
        assert a["repo"] == b["repo"] and a["ref"] != b["ref"]
        assert "private words" in json.dumps(fb.summary(share=True, notes=True))

    def test_the_commands(self, tmp_path):
        r = CliRunner()
        res = r.invoke(app, ["feedback", "record", "known", "--repo", "orders", "--note", "knew"])
        assert res.exit_code == 0 and "kept: known for orders" in res.output
        res = r.invoke(app, ["feedback", "record", "fine"])
        assert res.exit_code == 2
        out = tmp_path / "share.json"
        res = r.invoke(app, ["feedback", "summary", "--share", "--out", str(out)])
        assert res.exit_code == 0 and "Nothing was sent" in res.output
        assert "orders" not in out.read_text()
