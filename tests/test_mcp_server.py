"""The MCP server: JSON-RPC framing, the server-side allow-list, the
hash-pinned immutable proposal, the single-use approval, the ungated kill
switch, one audit row per call and no secret in any outbound payload."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from signald.mcp.server import McpServer, ProposalRefused
from signald.schema import sha256_of
from signald.stores import AuditChain

pytestmark = pytest.mark.timeout(120)

PROPOSAL = {
    "symbol": "AAPL",
    "side": "buy",
    "intent": "open",
    "rationale": "opening range breakout",
}


def make(
    cfg,
    clock,
    calls,
    state,
    *,
    toolsets="read,simulate,propose,mutate,config",
    simulate=None,
    propose=None,
    proposals_path=None,
    approvals_path=None,
    audit=None,
    ttl=900.0,
    pinned_descriptions=None,
):
    def record(name, result):
        def fn(payload=None):
            calls.append((name, payload))
            return result

        return fn

    def allow(payload):
        return {"verdict": "ALLOW", "binding_gate": None, "payload": payload}

    return McpServer(
        toolsets=toolsets,
        state=lambda: state,
        simulate=simulate or allow,
        propose=propose,
        submit=record("submit", {"broker_order_id": "b-1"}),
        cancel=record("cancel", {"cancelled": True}),
        halt=record("halt", {"halted": True}),
        set_allocation=record("set_allocation", {"sleeve": "intraday"}),
        proposals_path=proposals_path,
        approvals_path=approvals_path,
        audit=audit if audit is not None else AuditChain(cfg.audit_file, cfg.now),
        now=lambda: clock["now"],
        approval_ttl_s=ttl,
        pinned_descriptions=pinned_descriptions,
    )


def body(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def clock(now):
    return {"now": now}


@pytest.fixture
def calls():
    return []


@pytest.fixture
def state():
    return {
        "account": {"cash": 80_000.0, "equity": 110_000.0, "alpaca_secret": "SUPERSECRET"},
        "positions": [{"symbol": "AAPL", "qty": 1.0}],
        "orders": [{"order_id": "o-1", "status": "new"}],
        "risk": {"es_pct": 0.004, "drawdown_rung": 0},
        "sleeves": {"swing": {"capital_pct": 0.70}, "intraday": {"capital_pct": 0.30}},
        "signals": {"AAPL": {"signal_id": "s-1", "action": "BUY"}},
    }


# --- JSON-RPC framing ------------------------------------------------------
def test_initialize_framing(cfg, clock, calls, state):
    reply = make(cfg, clock, calls, state).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert reply["jsonrpc"] == "2.0" and reply["id"] == 1
    result = reply["result"]
    assert result["protocolVersion"] and result["serverInfo"]["name"] == "signald-mcp"
    assert "tools" in result["capabilities"]


def test_tools_list_framing(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state, toolsets="read,simulate,propose")
    reply = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = reply["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "get_account_state", "get_positions", "get_open_orders", "get_signal_latest",
        "get_risk_state", "get_sleeve_allocation", "simulate_order", "propose_order",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert set(tool["annotations"]) == {
            "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"
        }


def test_tools_call_framing(cfg, clock, calls, state):
    reply = make(cfg, clock, calls, state).handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_account_state", "arguments": {}}}
    )
    result = reply["result"]
    assert result["isError"] is False
    assert body(result)["account"]["equity"] == 110_000.0


def test_unknown_method_and_notification_framing(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state)
    unknown = server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert unknown["error"]["code"] == -32601
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) == {}
    bad = server.handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}})
    assert bad["error"]["code"] == -32602


# --- allow-list ------------------------------------------------------------
def test_an_unlisted_tool_is_not_callable(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state, toolsets="read")
    listed = {t["name"] for t in server.list_tools()}
    assert "submit_order" not in listed and "set_sleeve_allocation" not in listed
    result = server.call("submit_order", {"proposal_id": "p", "approval_id": "a"})
    assert result["isError"] is True and body(result)["error"] == "tool_not_found"
    assert server.call("set_sleeve_allocation", {"sleeve": "intraday", "capital_pct": 0.3,
                                                 "approval_id": ""})["isError"] is True


def test_halt_trading_is_the_ungated_exception(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state, toolsets="read")
    assert "halt_trading" not in {t["name"] for t in server.list_tools()}
    result = server.call("halt_trading", {})
    assert result["isError"] is False
    assert body(result)["result"]["halted"] is True
    assert ("halt", None) in calls


# --- propose ---------------------------------------------------------------
def test_a_blocked_simulate_creates_no_proposal(cfg, clock, calls, state, tmp_path):
    path = tmp_path / "proposals.jsonl"

    def blocked(payload):
        return {"verdict": "BLOCK", "binding_gate": "house_drawdown", "reasons": ["halt"]}

    server = make(cfg, clock, calls, state, simulate=blocked, proposals_path=path)
    with pytest.raises(ProposalRefused) as exc:
        server.propose_order(PROPOSAL)
    assert exc.value.code == "blocked"
    assert exc.value.verdict["binding_gate"] == "house_drawdown"

    result = server.call("propose_order", PROPOSAL)
    assert result["isError"] is True
    assert body(result)["verdict"]["binding_gate"] == "house_drawdown"
    assert not path.exists()


def test_propose_order_refuses_an_llm_number(cfg, clock, calls, state, tmp_path):
    path = tmp_path / "proposals.jsonl"
    server = make(cfg, clock, calls, state, proposals_path=path)
    payload = {**PROPOSAL, "qty": 100}
    with pytest.raises(ProposalRefused) as exc:
        server.propose_order(payload)
    assert exc.value.code == "llm_number"
    assert server.call("propose_order", payload)["isError"] is True
    assert not path.exists()


def test_a_simulate_failure_creates_no_proposal(cfg, clock, calls, state, tmp_path):
    path = tmp_path / "proposals.jsonl"

    def unavailable(payload):
        return None  # honest degradation: no gate verdict, no proposal

    server = make(cfg, clock, calls, state, simulate=unavailable, proposals_path=path)
    result = server.call("propose_order", PROPOSAL)
    assert result["isError"] is True and body(result)["error"] == "simulate_unavailable"
    assert not path.exists()


def test_the_proposal_is_hash_pinned_and_immutable(cfg, clock, calls, state, tmp_path):
    path = tmp_path / "proposals.jsonl"
    server = make(cfg, clock, calls, state, proposals_path=path)
    payload = dict(PROPOSAL)
    proposal = server.propose_order(payload)
    assert proposal.payload_hash == sha256_of(payload)
    assert proposal.state == "pending" and proposal.created_by == "llm"

    lines = path.read_text(encoding="utf-8").splitlines()
    stored = json.loads(lines[0])
    assert stored["payload_hash"] == sha256_of(stored["payload"])

    payload["symbol"] = "TSLA"  # the caller mutating their dict changes nothing
    assert stored["payload"]["symbol"] == "AAPL"

    # a fresh server recovers the immutable proposal and can approve it
    reloaded = make(cfg, clock, calls, state, proposals_path=path)
    approval = reloaded.approve(proposal.proposal_id, approved_by="operator")
    assert approval.payload_hash == proposal.payload_hash
    assert path.read_text(encoding="utf-8").splitlines() == lines


def test_a_tampered_proposal_row_is_not_a_proposal(cfg, clock, calls, state, tmp_path):
    path = tmp_path / "proposals.jsonl"
    server = make(cfg, clock, calls, state, proposals_path=path)
    proposal = server.propose_order(PROPOSAL)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["payload"]["symbol"] = "TSLA"  # the pin no longer matches
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

    reloaded = make(cfg, clock, calls, state, proposals_path=path)
    with pytest.raises(ValueError, match="unknown proposal"):
        reloaded.approve(proposal.proposal_id, approved_by="operator")


# --- submit ----------------------------------------------------------------
def test_submit_succeeds_only_with_the_matching_single_use_approval(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state)
    proposal = server.propose_order(PROPOSAL)
    approval = server.approve(proposal.proposal_id, approved_by="operator")

    ok = server.call("submit_order", {"proposal_id": proposal.proposal_id,
                                      "approval_id": approval.approval_id})
    assert ok["isError"] is False
    assert body(ok)["result"]["broker_order_id"] == "b-1"
    assert ("submit", {"proposal_id": proposal.proposal_id,
                       "approval_id": approval.approval_id}) in calls

    again = server.call("submit_order", {"proposal_id": proposal.proposal_id,
                                         "approval_id": approval.approval_id})
    assert again["isError"] is True and body(again)["error"] == "approval_used"

    empty = server.call("submit_order", {"proposal_id": proposal.proposal_id, "approval_id": ""})
    assert body(empty)["error"] == "approval_required"

    unknown = server.call("submit_order", {"proposal_id": proposal.proposal_id,
                                           "approval_id": "appr_nope"})
    assert body(unknown)["error"] == "approval_unknown"


def test_a_modified_proposal_is_a_different_proposal(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state)
    first = server.propose_order(PROPOSAL)
    approval = server.approve(first.proposal_id, approved_by="operator")
    modified = {**PROPOSAL, "rationale": "a different rationale"}
    second = server.propose_order(modified)
    assert second.payload_hash != first.payload_hash

    result = server.call("submit_order", {"proposal_id": second.proposal_id,
                                          "approval_id": approval.approval_id})
    assert result["isError"] is True and body(result)["error"] == "hash_mismatch"
    assert not any(name == "submit" for name, _ in calls)


def test_an_expired_approval_fails_on_the_injected_clock(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state, ttl=10.0)
    proposal = server.propose_order(PROPOSAL)
    approval = server.approve(proposal.proposal_id, approved_by="operator")
    clock["now"] = clock["now"] + timedelta(seconds=11)
    result = server.call("submit_order", {"proposal_id": proposal.proposal_id,
                                          "approval_id": approval.approval_id})
    assert result["isError"] is True and body(result)["error"] == "approval_expired"


def test_a_mutating_tool_without_a_matching_approval_never_reaches_the_broker(
    cfg, clock, calls, state
):
    server = make(cfg, clock, calls, state)
    proposal = server.propose_order(PROPOSAL)
    result = server.call("submit_order", {"proposal_id": proposal.proposal_id,
                                          "approval_id": "appr_missing"})
    assert result["isError"] is True
    assert not any(name == "submit" for name, _ in calls)


# --- kill switch and config ------------------------------------------------
def test_halt_trading_works_with_only_read_listed(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state, toolsets="read")
    result = server.call("halt_trading", {})
    assert result["isError"] is False and body(result)["result"]["halted"] is True


def test_set_sleeve_allocation_requires_an_operator_approval(cfg, clock, calls, state):
    server = make(cfg, clock, calls, state, toolsets="read,propose,config")
    args = {"sleeve": "intraday", "capital_pct": 0.35, "approval_id": ""}
    assert body(server.call("set_sleeve_allocation", args))["error"] == "approval_required"
    assert not any(name == "set_allocation" for name, _ in calls)

    proposal = server.propose_order(PROPOSAL)
    llm = server.approve(proposal.proposal_id, approved_by="llm")
    refused = server.call("set_sleeve_allocation",
                          {**args, "approval_id": llm.approval_id})
    assert body(refused)["error"] == "approval_operator"

    operator = server.approve(proposal.proposal_id, approved_by="operator")
    ok = server.call("set_sleeve_allocation", {**args, "approval_id": operator.approval_id})
    assert ok["isError"] is False
    assert body(ok)["result"] == {"sleeve": "intraday"}

    again = server.call("set_sleeve_allocation", {**args, "approval_id": operator.approval_id})
    assert body(again)["error"] == "approval_used"


# --- audit and secrets -----------------------------------------------------
def test_one_audit_row_per_call_with_no_model_free_text(cfg, clock, calls, state, tmp_path):
    audit = AuditChain(tmp_path / "audit.jsonl", cfg.now)
    server = make(cfg, clock, calls, state, audit=audit)
    server.call("get_account_state", {})
    server.call("propose_order", PROPOSAL)
    server.call("does_not_exist", {})

    rows = audit.read()
    assert [r["kind"] for r in rows] == ["mcp", "mcp", "mcp"]
    assert [r["reason"] for r in rows] == ["ok", "created", "not_found"]
    assert all(set(r["data"]) == {"tool", "toolset", "outcome", "proposal_id"} for r in rows)
    assert rows[1]["data"]["tool"] == "propose_order"
    assert str(rows[1]["data"]["proposal_id"]).startswith("prop_")
    assert rows[2]["data"]["tool"] == "does_not_exist" and rows[2]["data"]["toolset"] is None
    assert "opening range breakout" not in json.dumps(rows)


def test_no_outbound_payload_contains_a_secret(cfg, clock, calls, state, tmp_path):
    audit = AuditChain(tmp_path / "audit.jsonl", cfg.now)
    server = make(cfg, clock, calls, state, audit=audit)
    account = server.call("get_account_state", {})
    assert "SUPERSECRET" not in account["content"][0]["text"]
    assert "SUPERSECRET" not in json.dumps(server.list_tools())
    assert "SUPERSECRET" not in json.dumps(audit.read())


def test_the_description_pin_is_enforced_at_construction(cfg, clock, calls, state):
    from signald.mcp.tools import TOOL_TABLE, description_hash

    pinned = {s.name: description_hash(s) for s in TOOL_TABLE}
    make(cfg, clock, calls, state, pinned_descriptions=pinned)  # unchanged: starts
    pinned["halt_trading"] = "0" * 64
    with pytest.raises(ValueError, match="halt_trading"):
        make(cfg, clock, calls, state, pinned_descriptions=pinned)


def test_an_unknown_toolset_refuses_to_start(cfg, clock, calls, state):
    with pytest.raises(ValueError, match="admin"):
        make(cfg, clock, calls, state, toolsets="read,admin")


def test_the_mcp_bind_refuses_a_non_loopback_host():
    from signald.mcp.server import _is_loopback, _split_bind

    assert _is_loopback("127.0.0.1") and _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0")
    with pytest.raises(ValueError):
        _split_bind("0.0.0.0:8787")
    assert _split_bind("127.0.0.1:8787") == ("127.0.0.1", 8787)
