"""CLI smoke tests (in-process main()) — no network, seam-injected."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from signald import cli
from signald.samples import build_sample

pytestmark = pytest.mark.timeout(120)


def test_init_mandate(tmp_path):
    target = tmp_path / "mandate.json"
    rc = cli.main(["init-mandate", "--mandate", str(target)])
    assert rc == 0
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["id"] == "mandate-phaseA"
    assert doc["hash"]  # re-signed


def test_sample_writes_valid_artifact(tmp_path):
    out = tmp_path / "decisions" / "research_decision.json"
    rc = cli.main(["sample", "--out", str(out)])
    assert rc == 0
    from signald.schema import parse_research_decision

    rd = parse_research_decision(json.loads(out.read_text(encoding="utf-8")))
    assert rd.ticker == "AVGO" and rd.action() == "REDUCE"


def test_run_without_keys_refuses(tmp_path, capsys, monkeypatch):
    from signald.mandate import DEFAULT_MANDATE, write_mandate

    for k in ("TRADINGAGENTS_ALPACA_API_KEY_ID", "TRADINGAGENTS_ALPACA_API_SECRET",
              "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)

    cfg_path = tmp_path / ".env"
    write_mandate(tmp_path / "mandate.json", DEFAULT_MANDATE)
    rc = cli.main(["run", "--once", "--watch", str(tmp_path / "decisions"),
                   "--data", str(tmp_path / "signals"), "--env", str(cfg_path),
                   "--mandate", str(tmp_path / "mandate.json")])
    assert rc == 1
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_run_once_execute_end_to_end(tmp_path, monkeypatch, capsys, seam):
    """Full pipeline through the CLI with the transport seam injected."""
    from signald.alpaca_ref import AlpacaReference
    from signald.mandate import DEFAULT_MANDATE, write_mandate

    watch = tmp_path / "decisions"
    data = tmp_path / "signals"
    cfg_path = tmp_path / ".env"
    watch.mkdir()
    cfg_path.write_text("ALPACA_API_KEY=dummy\nALPACA_SECRET_KEY=dummy\n", encoding="utf-8")
    write_mandate(tmp_path / "mandate.json", DEFAULT_MANDATE)
    (watch / "AVGO_decision.json").write_text(
        json.dumps(build_sample(), sort_keys=True), encoding="utf-8"
    )

    monkeypatch.setattr(cli, "AlpacaReference",
                        lambda *a, **k: AlpacaReference(transport=seam))

    rc = cli.main(["run", "--once", "--execute", "--watch", str(watch),
                   "--data", str(data), "--env", str(cfg_path),
                   "--mandate", str(tmp_path / "mandate.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "emitted" in out
    rows = (data / "signals.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["ticker"] == "AVGO"


def test_verify_cli(tmp_path):
    from signald.stores import AuditChain

    audit_path = tmp_path / "audit.jsonl"
    audit = AuditChain(audit_path, lambda: datetime(2026, 9, 3, 12, 0))
    audit.append("accepted", "test", ticker="AVGO")
    rc = cli.main(["verify", "--audit", str(audit_path)])
    assert rc == 0
    # tamper -> exit 1
    rows = audit_path.read_text(encoding="utf-8").splitlines()
    tampered = rows[0].replace('"reason": "test"', '"reason": "MUTATED"')
    audit_path.write_text(tampered, encoding="utf-8")
    assert cli.main(["verify", "--audit", str(audit_path)]) == 1
