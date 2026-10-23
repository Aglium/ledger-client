"""Nothing blocks a merge until a deployment says which verdicts do."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ledger import cli as cli_mod
from ledger.cli import app
from ledger.config import Config
from ledger.core.brief import build_brief
from ledger.core.gate import blocks, validate_enforce
from ledger.core.join import Change, find_siblings


class TestTheRule:
    def test_nothing_is_enforced_by_default(self):
        assert not any(blocks(v, ()) for v in ("proceed", "review_with", "decision_needed",
                                               "insufficient_data"))

    def test_only_the_named_classes_block(self):
        assert blocks("decision_needed", ("decision_needed",))
        assert not blocks("review_with", ("decision_needed",))

    def test_enforcing_a_stop_does_not_imply_stopping_on_unknown(self):
        assert not blocks("insufficient_data", ("decision_needed", "review_with"))

    def test_proceed_cannot_be_enforced(self):
        with pytest.raises(ValueError, match="not proceed"):
            validate_enforce(["proceed"])

    def test_a_typo_is_refused_rather_than_ignored(self):
        with pytest.raises(ValueError):
            validate_enforce(["decision-needed"])


def config(tmp_path, enforce):
    p = tmp_path / "ledger.yml"
    p.write_text(f"org: acme\nproject_keys: [ABC]\nenforce: {enforce}\n")
    return str(p)


def brief(recommendation):
    me = Change(repo="orders", ref="feat/abc-1-x", tickets=("ABC-1",))
    others = [Change(repo="billing", ref="feat/abc-1-y", tickets=("ABC-1",))]
    b = build_brief(find_siblings(me, [me] + (others if recommendation != "proceed" else [])), [],
                    resolved=True, base="main", own_commits=1)
    assert b.recommendation == recommendation
    return b


class TestTheCommand:
    def run(self, monkeypatch, tmp_path, enforce, recommendation, *flags):
        monkeypatch.delenv("LEDGER_URL", raising=False)
        monkeypatch.setattr(cli_mod, "_adapters", lambda cfg, root: [])
        monkeypatch.setattr(cli_mod.service, "run_brief", lambda *a, **k: brief(recommendation))
        return CliRunner().invoke(app, ["brief", "orders", "feat/abc-1-x", "--config",
                                        config(tmp_path, enforce), "--json", *flags])

    def test_without_the_flag_a_verdict_never_fails_the_command(self, monkeypatch, tmp_path):
        assert self.run(monkeypatch, tmp_path, "[review_with]", "review_with").exit_code == 0

    def test_with_the_flag_an_enforced_verdict_exits_1(self, monkeypatch, tmp_path):
        res = self.run(monkeypatch, tmp_path, "[review_with]", "review_with", "--gate")
        assert res.exit_code == 1 and '"recommendation": "review_with"' in res.output

    def test_with_the_flag_and_nothing_enforced_it_passes(self, monkeypatch, tmp_path):
        assert self.run(monkeypatch, tmp_path, "[]", "review_with", "--gate").exit_code == 0

    def test_a_verdict_that_is_not_enforced_passes(self, monkeypatch, tmp_path):
        assert self.run(monkeypatch, tmp_path, "[decision_needed]", "review_with", "--gate").exit_code == 0


def test_the_config_refuses_an_unknown_class(tmp_path):
    with pytest.raises(ValueError):
        Config.load(config(tmp_path, "[blocked]"))


class TestTheCheckRun:
    def post(self, monkeypatch, tmp_path, enforce, recommendation, capsys):
        from ledger.cli import post_comment
        me = Change(repo="orders", ref="feat/abc-1-x", tickets=("ABC-1",))
        result = find_siblings(me, [me])
        with pytest.raises(Exception):                   # typer.Exit after printing the dry run
            post_comment(Config.load(config(tmp_path, enforce)), "orders", 1, result, dry_run=True,
                         local_root=None, head_sha=None, brief=brief(recommendation))
        return capsys.readouterr().out

    def test_it_fails_on_an_enforced_verdict(self, monkeypatch, tmp_path, capsys):
        assert "check: failure" in self.post(monkeypatch, tmp_path, "[review_with]", "review_with", capsys)

    def test_it_stays_advisory_on_one_that_is_not(self, monkeypatch, tmp_path, capsys):
        out = self.post(monkeypatch, tmp_path, "[decision_needed]", "review_with", capsys)
        assert "check: failure" not in out
