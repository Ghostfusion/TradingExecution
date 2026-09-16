"""Launcher tests: `signald api` / `signald mcp` over the real seams (plan §7).

No sockets are opened: the API is driven through ``ControlApi.handle`` and the
MCP surface through ``handle_message``, both wrapped by the launcher's own
wiring. The refusal cases (switch off, incomplete key, non-loopback bind) must
stop before a socket exists.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from signald import cli
from signald.api.server import Request
from signald.api.signing import sign
from signald.control import (
    ControlSurfaceError,
    build_control_api,
    build_mcp_server,
    local_state,
)
from signald.stores import AuditChain, SignalStore

pytestmark = pytest.mark.timeout(120)

KEY_ID = "op-1"
SECRET = "signing-secret"
SIGNAL = {"ticker": "AVGO", "signal_id": "sg-1", "action": "REDUCE"}
PENDING_ORDER = {"client_order_id": "cid-1", "symbol": "NVGO", "status": "pending"}


def _keyed(cfg, **overrides):
    """The config a launcher needs: the switch on (default) plus its signing key."""
    fields = {"api_key_id": KEY_ID, "api_signing_secret": SECRET}
    fields.update(overrides)
    return replace(cfg, **fields)


def _seed(cfg) -> None:
    """Local durable state: one signal and one pending order."""
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    store.append(SIGNAL)
    store.write_latest(SIGNAL)
    (cfg.data_dir / "orders_pending.jsonl").write_text(
        json.dumps(PENDING_ORDER) + "\n", encoding="utf-8"
    )


def _call(api, cfg, method, path, *, body=b"", query=None, nonce="n1"):
    """One signed request, built with the same primitives the server verifies."""
    stamp = cfg.now().isoformat()
    headers = {
        "X-Key-Id": KEY_ID,
        "X-Timestamp": stamp,
        "X-Nonce": nonce,
        "X-Signature": sign(
            SECRET, method=method, path=path, body=body, timestamp=stamp,
            nonce=nonce, key_id=KEY_ID,
        ),
    }
    request = Request(
        method=method, path=path, query=dict(query or {}), body=body, headers=headers
    )
    return api.handle(request)


def _rpc(server, method, params=None, mid=1):
    message = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle_message(message)


def _body(result) -> dict:
    return json.loads(result["content"][0]["text"])


# --- refusals (fail closed before serving) ---------------------------------
def test_the_api_launcher_refuses_when_its_switch_is_off(cfg):
    with pytest.raises(ControlSurfaceError, match="API_ENABLED"):
        build_control_api(_keyed(cfg, api_enabled=False))


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key_id": None},
        {"api_signing_secret": None},
    ],
)
def test_the_api_launcher_refuses_without_a_complete_signing_key(cfg, overrides):
    """An unauthenticated control surface is not a surface (plan §7.2)."""
    with pytest.raises(ControlSurfaceError, match="API_KEY_ID"):
        build_control_api(_keyed(cfg, **overrides))


def test_the_mcp_launcher_refuses_when_its_switch_is_off(cfg):
    with pytest.raises(ControlSurfaceError, match="MCP_ENABLED"):
        build_mcp_server(replace(cfg, mcp_enabled=False))


@pytest.mark.parametrize("bind", ["0.0.0.0:8787", "192.168.1.10:8787"])
def test_the_api_refuses_a_non_loopback_bind_before_any_socket(cfg, bind):
    api = build_control_api(_keyed(cfg, api_bind=bind))
    with pytest.raises(ValueError, match="loopback"):
        api.serve()


@pytest.mark.parametrize("bind", ["0.0.0.0", "0.0.0.0:8787"])
def test_mcp_refuses_a_non_loopback_bind_before_any_socket(cfg, bind):
    with pytest.raises(ValueError, match="loopback"):
        build_mcp_server(cfg).serve(bind=bind)


def test_the_cli_reports_which_switch_stopped_it(tmp_path, capsys):
    """`signald api` / `signald mcp` exit 1 with the reason on stderr."""
    env = tmp_path / ".env"
    env.write_text(
        "TRADINGEXEC_API_ENABLED=false\nTRADINGEXEC_MCP_ENABLED=false\n", encoding="utf-8"
    )

    assert cli.main(["api", "--env", str(env)]) == 1
    out = capsys.readouterr()
    assert "error:" in out.err and "API_ENABLED" in out.err

    assert cli.main(["mcp", "--env", str(env)]) == 1
    out = capsys.readouterr()
    assert "error:" in out.err and "MCP_ENABLED" in out.err


def test_the_cli_refuses_a_non_loopback_bind_without_binding(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(
        "TRADINGEXEC_API_ENABLED=true\nTRADINGEXEC_API_KEY_ID=op-1\n"
        "TRADINGEXEC_API_SIGNING_SECRET=s1\nTRADINGEXEC_API_BIND=0.0.0.0:8787\n",
        encoding="utf-8",
    )
    assert cli.main(["api", "--env", str(env)]) == 1
    assert "loopback" in capsys.readouterr().err

    env.write_text(
        "TRADINGEXEC_MCP_ENABLED=true\nTRADINGEXEC_MCP_BIND=0.0.0.0:8787\n", encoding="utf-8"
    )
    assert cli.main(["mcp", "--env", str(env)]) == 1
    assert "loopback" in capsys.readouterr().err


# --- the API over the real seams --------------------------------------------
def test_the_read_routes_serve_local_state_and_name_what_is_missing(cfg):
    """Local state is served; a broker-backed resource is null, never invented."""
    _seed(cfg)
    api = build_control_api(_keyed(cfg))

    latest = _call(api, cfg, "GET", "/v1/signals/latest", query={"symbol": "AVGO"})
    assert latest.status == 200 and latest.payload == {"symbol": "AVGO", "signal": SIGNAL}

    orders = _call(api, cfg, "GET", "/v1/orders/open", nonce="n2")
    assert orders.status == 200 and orders.payload == {"orders": [PENDING_ORDER]}

    account = _call(api, cfg, "GET", "/v1/state/account", nonce="n3")
    assert account.status == 200 and account.payload == {"account": None}


def test_an_unwired_order_route_answers_503_rather_than_half_wiring(cfg):
    api = build_control_api(_keyed(cfg))
    body = json.dumps({"proposal_id": "p1", "approval_id": "a1"}).encode()
    response = _call(api, cfg, "POST", "/v1/orders/submit", body=body)
    assert response.status == 503 and response.payload == {"error": "api_disabled"}


def test_the_api_halt_engages_the_same_kill_switch_as_the_cli(cfg):
    api = build_control_api(_keyed(cfg))

    response = _call(api, cfg, "POST", "/v1/halt")

    assert response.status == 200 and response.payload["halted"] is True
    assert cfg.kill_switch_path.exists()
    assert f"api:{KEY_ID}" in cfg.kill_switch_path.read_text(encoding="utf-8")
    episode = json.loads(cfg.halt_latch_path.read_text(encoding="utf-8"))
    assert episode["episode"] == response.payload["episode"]
    kinds = [row["kind"] for row in AuditChain(cfg.audit_file, cfg.now).read()]
    assert "kill_switch" in kinds


def test_local_state_reports_the_kill_switch_and_config_hash(cfg):
    state = local_state(cfg)

    assert state["kill_switch"] == {"halted": False, "episode": None}
    assert state["config_hash"] == cfg.config_hash()
    assert state["signals"] == {} and state["orders"] == []


# --- the MCP surface over the real seams ------------------------------------
def test_mcp_serves_the_read_tools_from_local_state(cfg):
    _seed(cfg)
    server = build_mcp_server(cfg)

    listed = _rpc(server, "tools/list")
    assert "get_open_orders" in {tool["name"] for tool in listed["result"]["tools"]}

    orders = _rpc(server, "tools/call", {"name": "get_open_orders", "arguments": {}})
    assert orders["result"]["isError"] is False
    assert _body(orders["result"]) == {"orders": [PENDING_ORDER]}

    signal = _rpc(
        server, "tools/call",
        {"name": "get_signal_latest", "arguments": {"symbol": "AVGO"}}, mid=2,
    )
    assert _body(signal["result"]) == {"symbol": "AVGO", "signal": SIGNAL}


def test_mcp_names_the_effects_it_cannot_serve(cfg):
    """No seam, no effect - and saying so beats a half-wired order path."""
    server = build_mcp_server(cfg)

    # `mutate` is not in the default toolsets, so the tool is not even offered.
    submitted = _rpc(
        server, "tools/call",
        {"name": "submit_order", "arguments": {"proposal_id": "p1", "approval_id": "a1"}},
    )
    assert _body(submitted["result"]) == {"error": "tool_not_found", "tool": "submit_order"}

    # Listing `mutate` still gets an honest refusal: there is no broker adapter.
    widened = build_mcp_server(replace(cfg, mcp_toolsets="read,simulate,propose,mutate,config"))
    assert _body(
        _rpc(
            widened, "tools/call",
            {"name": "submit_order", "arguments": {"proposal_id": "p1", "approval_id": "a1"}},
            mid=2,
        )["result"]
    ) == {"error": "submit_unavailable"}

    proposed = _rpc(
        server, "tools/call",
        {"name": "propose_order",
         "arguments": {"symbol": "AVGO", "side": "buy", "intent": "open"}},
        mid=4,
    )
    assert _body(proposed["result"])["error"] == "simulate_unavailable"
    assert not (cfg.audit_file.parent / "proposals.jsonl").exists()


def test_mcp_halt_engages_the_kill_switch(cfg):
    server = build_mcp_server(cfg)

    halted = _rpc(server, "tools/call", {"name": "halt_trading", "arguments": {}})

    assert halted["result"]["isError"] is False
    assert _body(halted["result"])["result"]["halted"] is True
    assert cfg.kill_switch_path.exists()
    assert "mcp" in cfg.kill_switch_path.read_text(encoding="utf-8")
