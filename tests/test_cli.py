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
    from signald.watch import ARTIFACT_NAME

    (watch / ARTIFACT_NAME).write_text(
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


def _mandate_fixture(tmp_path, allowed=None):
    from signald.mandate import DEFAULT_MANDATE, write_mandate

    path = tmp_path / "mandate.json"
    doc = json.loads(json.dumps(DEFAULT_MANDATE))
    if allowed is not None:
        doc["symbols"]["allowed"] = list(allowed)
    write_mandate(path, doc)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return path


def test_mandate_add_resigns_and_records_provenance(tmp_path, capsys):
    from signald.mandate import load_mandate

    path = _mandate_fixture(tmp_path)
    before = load_mandate(path)

    rc = cli.main(["mandate-add", "NFLX", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals"),
                   "--operator", "vince", "--reason", "buy rating"])

    assert rc == 0
    after = load_mandate(path)
    assert "NFLX" in after.allowed and after.hash != before.hash
    archive = [json.loads(l) for l in
               (tmp_path / "mandate.json.archive.jsonl").read_text(encoding="utf-8").splitlines()]
    assert archive[-1]["ticker"] == "NFLX" and archive[-1]["action"] == "mandate_add"
    assert archive[-1]["operator"] == "vince" and archive[-1]["reason"] == "buy rating"
    assert archive[-1]["old_hash"] == before.hash and archive[-1]["new_hash"] == after.hash
    rows = [json.loads(l) for l in
            (tmp_path / "signals" / "audit" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in rows] == ["mandate_added"]
    assert rows[0]["data"]["new_hash"] == after.hash
    out = capsys.readouterr().out
    assert "added NFLX" in out and "next poll" in out


def test_mandate_add_is_a_no_op_when_already_allowed(tmp_path, capsys):
    from signald.mandate import load_mandate

    path = _mandate_fixture(tmp_path)
    before = load_mandate(path)

    rc = cli.main(["mandate-add", "SPY", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals")])

    assert rc == 0 and "already allowed" in capsys.readouterr().out
    assert load_mandate(path).hash == before.hash
    assert not (tmp_path / "mandate.json.archive.jsonl").exists()


def test_mandate_add_rejects_an_implausible_ticker(tmp_path, capsys):
    from signald.mandate import load_mandate

    path = _mandate_fixture(tmp_path)

    rc = cli.main(["mandate-add", "not a ticker", "--mandate", str(path),
                   "--env", str(tmp_path / ".env")])

    assert rc == 1 and "plausible" in capsys.readouterr().err
    assert "NFLX" not in load_mandate(path).allowed


def test_mandate_remove_refuses_a_held_symbol_without_force(
    tmp_path, capsys, monkeypatch, seam, transport_state
):
    from signald.alpaca_ref import AlpacaReference
    from signald.mandate import load_mandate

    transport_state["positions"] = {"positions_value": 500.0, "symbols": ["NFLX"]}
    monkeypatch.setattr(cli, "AlpacaReference", lambda *a, **k: AlpacaReference(transport=seam))
    path = _mandate_fixture(tmp_path, allowed=["SPY", "NFLX"])

    rc = cli.main(["mandate-remove", "NFLX", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals")])

    assert rc == 1 and "positions" in capsys.readouterr().err
    assert "NFLX" in load_mandate(path).allowed


def test_mandate_remove_force_removes_a_held_symbol(
    tmp_path, capsys, monkeypatch, seam, transport_state
):
    from signald.alpaca_ref import AlpacaReference
    from signald.mandate import load_mandate

    transport_state["positions"] = {"positions_value": 500.0, "symbols": ["NFLX"]}
    monkeypatch.setattr(cli, "AlpacaReference", lambda *a, **k: AlpacaReference(transport=seam))
    path = _mandate_fixture(tmp_path, allowed=["SPY", "NFLX"])

    rc = cli.main(["mandate-remove", "NFLX", "--force", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals")])

    assert rc == 0
    assert "NFLX" not in load_mandate(path).allowed
    out = capsys.readouterr().out
    assert "removed NFLX" in out and "still held" in out
    archive = [json.loads(l) for l in
               (tmp_path / "mandate.json.archive.jsonl").read_text(encoding="utf-8").splitlines()]
    assert archive[-1]["held"] is True and archive[-1]["action"] == "mandate_remove"


def test_mandate_remove_needs_force_when_holdings_are_unknown(tmp_path, capsys):
    from signald.mandate import load_mandate

    path = _mandate_fixture(tmp_path, allowed=["SPY", "NFLX"])

    rc = cli.main(["mandate-remove", "NFLX", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals")])

    assert rc == 1 and "cannot verify holdings" in capsys.readouterr().err
    assert "NFLX" in load_mandate(path).allowed


def test_mandate_remove_refuses_the_last_symbol(
    tmp_path, capsys, monkeypatch, seam, transport_state
):
    from signald.alpaca_ref import AlpacaReference
    from signald.mandate import load_mandate

    transport_state["positions"] = {"positions_value": 0.0, "symbols": []}
    monkeypatch.setattr(cli, "AlpacaReference", lambda *a, **k: AlpacaReference(transport=seam))
    path = _mandate_fixture(tmp_path, allowed=["NFLX"])

    rc = cli.main(["mandate-remove", "NFLX", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals")])

    assert rc == 1 and "non-empty" in capsys.readouterr().err
    assert load_mandate(path).allowed == frozenset({"NFLX"})


def test_mandate_remove_drops_an_unheld_symbol(
    tmp_path, capsys, monkeypatch, seam, transport_state
):
    from signald.alpaca_ref import AlpacaReference
    from signald.mandate import load_mandate

    transport_state["positions"] = {"positions_value": 0.0, "symbols": []}
    monkeypatch.setattr(cli, "AlpacaReference", lambda *a, **k: AlpacaReference(transport=seam))
    path = _mandate_fixture(tmp_path, allowed=["SPY", "NFLX"])

    rc = cli.main(["mandate-remove", "NFLX", "--mandate", str(path),
                   "--env", str(tmp_path / ".env"), "--data", str(tmp_path / "signals")])

    assert rc == 0 and "removed NFLX" in capsys.readouterr().out
    assert load_mandate(path).allowed == frozenset({"SPY"})


def test_verify_defaults_to_the_configured_ledger(tmp_path, capsys):
    """Bare `verify` resolves the configured ledger instead of building Path(None)."""
    from signald.stores import AuditChain

    data = tmp_path / "signals"
    AuditChain(data / "audit" / "audit.jsonl", lambda: datetime(2026, 9, 3, 12, 0)).append(
        "accepted", "test", ticker="AVGO"
    )

    rc = cli.main(["verify", "--data", str(data)])

    assert rc == 0 and "OK" in capsys.readouterr().out
