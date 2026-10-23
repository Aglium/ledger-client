"""The human-readable brief. A removed-contract section crashed the CLI on a
real branch because no test rendered one: every section is rendered here."""

from __future__ import annotations

from typer.testing import CliRunner

from ledger import service
from ledger.cli import app
from ledger.core.brief import ContractBreak, build_brief
from ledger.core.contracts import ContractRef, Surface
from ledger.core.join import Change, find_siblings


def test_breaks_and_parties_render(monkeypatch, tmp_path):
    cfg = tmp_path / "l.yml"
    cfg.write_text(f"org: t\nproject_keys: [ABC]\nlocal_root: {tmp_path}\n")
    monkeypatch.setenv("LEDGER_CONFIG", str(cfg))
    monkeypatch.delenv("LEDGER_DB_URL", raising=False)
    subj = Change(repo="orders", ref="feat/abc-1-v2", tickets=("ABC-1",))
    topic = ContractRef(Surface.TOPIC, "order.events.v1")
    b = build_brief(
        find_siblings(subj, [subj]), [], contracts=[topic], contract_owners={topic: ["billing"]},
        contract_breaks=[ContractBreak(surface="topic", name="order.events.v1", also_touched_by=["billing"],
                                       evidence='TOPIC = _topic("X_TOPIC", "order.events.v1")', path="app/topics.py")],
        contract_parties={topic: [{"repo": "billing", "uses": "consumes", "how": "declared"}]},
        own_commits=1)
    monkeypatch.setattr(service, "run_brief", lambda *a, **k: b)
    r = CliRunner().invoke(app, ["brief", "orders", "feat/abc-1-v2"])
    assert r.exit_code == 0, r.output
    assert "DECISION_NEEDED" in r.output
    assert "used by: billing" in r.output
    assert "consumed by billing" in r.output and "consumes   declared" in r.output
