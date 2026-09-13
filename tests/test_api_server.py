"""P5 control-API tests: routing, roles, limits, audit and loopback binding.

Every request is driven through ``ControlApi.handle`` directly -- the suite
opens no sockets. The signed material is produced with the same primitives the
server verifies, and the audit ledger is the observable record of each request.
"""

from __future__ import annotations

import json

import pytest

from signald.api.server import MAX_BODY_BYTES, ControlApi, Request, _split_bind
from signald.api.signing import Authenticator, NonceStore, sign
from signald.stores import AuditChain

pytestmark = pytest.mark.timeout(120)

SECRETS = {
    "k-read": "read-secret",
    "k-propose": "propose-secret",
    "k-execute": "execute-secret",
    "k-admin": "admin-secret",
}
ROLES = {
    "k-read": "read",
    "k-propose": "propose",
    "k-execute": "execute",
    "k-admin": "admin",
}

STATE = {
    "account": {"cash": 10_000.0, "equity": 50_000.0},
    "positions": [{"symbol": "AAPL", "qty": 10}],
    "risk": {"heat_pct": 0.0, "es_pct": 0.004},
    "sleeves": {"swing": {"deployed_pct": 0.2}},
    "signals": {"AAPL": {"signal_id": "s1", "action": "BUY"}},
    "orders": [{"client_order_id": "c1", "status": "new"}],
}


class Harness:
    """A built API plus a unique-nonce signed-request helper."""

    def __init__(self, api: ControlApi, audit: AuditChain, now) -> None:
        self.api = api
        self.audit = audit
        self.now = now
        self._n = 0

    def request(self, method, path, *, key="k-admin", body=b"", query=None, timestamp=None):
        self._n += 1
        nonce = f"n{self._n}"
        ts = (timestamp or self.now).isoformat()
        signature = sign(
            SECRETS[key], method=method, path=path, body=body, timestamp=ts,
            nonce=nonce, key_id=key,
        )
        headers = {
            "X-Key-Id": key,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        request = Request(
            method=method, path=path, query=dict(query or {}), body=body, headers=headers,
        )
        return self.api.handle(request)

    def raw(self, method, path, *, headers=None, body=b"", query=None):
        request = Request(
            method=method, path=path, query=dict(query or {}), body=body,
            headers=dict(headers or {}),
        )
        return self.api.handle(request)

    def audit_rows(self):
        return [r for r in self.audit.read() if r.get("kind") == "api_request"]


def make_api(tmp_path, now, *, enabled=True, bind="127.0.0.1:8787", **overrides) -> Harness:
    audit = AuditChain(tmp_path / "audit.jsonl", lambda: now)
    callbacks = {
        "state": lambda: STATE,
        "simulate": lambda body: {"verdict": "ALLOW"},
        "propose": lambda body: {"proposal_id": "p1"},
        "submit": lambda body: {"order_id": "o1"},
        "cancel": lambda body: {"canceled": True},
        "halt": lambda: {"halted": True},
        "ceiling": lambda sleeve, body: {
            "sleeve": sleeve,
            "capital_ceiling_pct": body.get("capital_ceiling_pct"),
        },
    }
    callbacks.update(overrides)
    authenticator = Authenticator(
        SECRETS, ROLES, nonce_store=NonceStore(None, now=lambda: now), now=lambda: now
    )
    api = ControlApi(
        authenticator=authenticator, audit=audit, enabled=enabled, bind=bind, **callbacks
    )
    return Harness(api, audit, now)


# --- authorised reads ------------------------------------------------------
@pytest.mark.parametrize("path,resource", [
    ("/v1/state/account", "account"),
    ("/v1/state/positions", "positions"),
    ("/v1/state/risk", "risk"),
    ("/v1/state/sleeves", "sleeves"),
    ("/v1/orders/open", "orders"),
])
def test_read_endpoints_return_their_resource(tmp_path, now, path, resource):
    harness = make_api(tmp_path, now)
    response = harness.request("GET", path, key="k-read")
    assert response.status == 200
    assert response.payload == {resource: STATE[resource]}


def test_signals_latest_returns_the_symbol_envelope(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request(
        "GET", "/v1/signals/latest", key="k-read", query={"symbol": "AAPL"}
    )
    assert response.status == 200
    assert response.payload == {"symbol": "AAPL", "signal": STATE["signals"]["AAPL"]}


def test_signals_latest_without_a_symbol_is_a_bad_request(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("GET", "/v1/signals/latest", key="k-read")
    assert response.status == 400
    assert response.payload == {"error": "missing_symbol"}


# --- authorised mutations --------------------------------------------------
def test_propose_returns_the_created_proposal(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("POST", "/v1/propose", key="k-propose", body=b"{}")
    assert response.status == 200
    assert response.payload == {"proposal_id": "p1"}


def test_simulate_returns_the_gate_verdict(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("POST", "/v1/simulate", key="k-propose", body=b"{}")
    assert response.status == 200
    assert response.payload == {"verdict": "ALLOW"}


def test_submit_with_an_approval_returns_the_order(tmp_path, now):
    harness = make_api(tmp_path, now)
    body = json.dumps({"proposal_id": "p1", "approval_id": "a1"}).encode()
    response = harness.request("POST", "/v1/orders/submit", key="k-execute", body=body)
    assert response.status == 200
    assert response.payload == {"order_id": "o1"}


def test_cancel_returns_its_acknowledgement(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("POST", "/v1/orders/cancel", key="k-execute", body=b"{}")
    assert response.status == 200
    assert response.payload == {"canceled": True}


def test_ceiling_update_passes_the_sleeve_and_body(tmp_path, now):
    harness = make_api(tmp_path, now)
    body = json.dumps({"capital_ceiling_pct": 0.5}).encode()
    response = harness.request("POST", "/v1/sleeves/swing/ceiling", key="k-admin", body=body)
    assert response.status == 200
    assert response.payload == {"sleeve": "swing", "capital_ceiling_pct": 0.5}


def test_halt_works_with_the_read_role(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("POST", "/v1/halt", key="k-read", body=b"{}")
    assert response.status == 200
    assert response.payload == {"halted": True}


# --- denial ----------------------------------------------------------------
@pytest.mark.parametrize("path,key", [
    ("/v1/propose", "k-read"),
    ("/v1/simulate", "k-read"),
    ("/v1/orders/submit", "k-propose"),
    ("/v1/orders/cancel", "k-propose"),
    ("/v1/sleeves/swing/ceiling", "k-execute"),
])
def test_endpoint_without_its_role_is_denied(tmp_path, now, path, key):
    harness = make_api(tmp_path, now)
    body = json.dumps({"proposal_id": "p1", "approval_id": "a1"}).encode()
    response = harness.request("POST", path, key=key, body=body)
    assert response.status in (401, 403)
    assert response.payload == {"error": "unauthorized"}


def test_a_read_without_credentials_is_401(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.raw("GET", "/v1/state/account")
    assert response.status == 401
    assert response.payload == {"error": "unauthorized"}


def test_a_read_with_an_unknown_key_is_401(tmp_path, now):
    harness = make_api(tmp_path, now)
    ts = now.isoformat()
    signature = sign(
        "ghost-secret", method="GET", path="/v1/state/account", body=b"",
        timestamp=ts, nonce="ghost", key_id="k-ghost",
    )
    headers = {
        "X-Key-Id": "k-ghost",
        "X-Timestamp": ts,
        "X-Nonce": "ghost",
        "X-Signature": signature,
    }
    response = harness.raw("GET", "/v1/state/account", headers=headers)
    assert response.status == 401
    row = harness.audit_rows()[-1]
    assert row["reason"] == "unknown_key"


def test_an_unknown_path_is_404(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("GET", "/v1/nope", key="k-read")
    assert response.status == 404
    assert response.payload == {"error": "not_found"}


def test_a_wrong_method_on_a_known_path_is_404(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request("GET", "/v1/propose", key="k-read")
    assert response.status == 404


# --- gating and limits -----------------------------------------------------
def test_a_disabled_api_is_503(tmp_path, now):
    harness = make_api(tmp_path, now, enabled=False)
    response = harness.request("GET", "/v1/state/account", key="k-read")
    assert response.status == 503
    assert response.payload == {"error": "api_disabled"}


def test_a_missing_handler_is_503(tmp_path, now):
    harness = make_api(tmp_path, now, simulate=None)
    response = harness.request("POST", "/v1/simulate", key="k-propose", body=b"{}")
    assert response.status == 503
    assert response.payload == {"error": "api_disabled"}


def test_submit_without_an_approval_id_is_400(tmp_path, now):
    harness = make_api(tmp_path, now)
    body = json.dumps({"proposal_id": "p1"}).encode()
    response = harness.request("POST", "/v1/orders/submit", key="k-execute", body=body)
    assert response.status == 400
    assert response.payload == {"error": "approval_required"}


def test_an_oversized_body_is_413(tmp_path, now):
    harness = make_api(tmp_path, now)
    response = harness.request(
        "POST", "/v1/propose", key="k-propose", body=b"x" * (MAX_BODY_BYTES + 1)
    )
    assert response.status == 413
    assert response.payload == {"error": "payload_too_large"}


# --- audit -----------------------------------------------------------------
def test_every_request_leaves_exactly_one_audit_row(tmp_path, now):
    harness = make_api(tmp_path, now)
    harness.request("GET", "/v1/state/account", key="k-read")
    harness.request("POST", "/v1/propose", key="k-propose", body=b"{}")
    harness.request("GET", "/v1/nope", key="k-read")
    assert len(harness.audit_rows()) == 3


def test_the_audit_row_carries_the_specific_auth_failure_reason(tmp_path, now):
    harness = make_api(tmp_path, now)
    harness.request("POST", "/v1/propose", key="k-read", body=b"{}")
    row = harness.audit_rows()[-1]
    assert row["reason"] == "forbidden"
    assert row["data"]["role"] == "read"
    assert row["data"]["path"] == "/v1/propose"
    assert row["data"]["outcome"] == "unauthorized"
    assert row["data"]["status"] == 403


def test_a_missing_header_is_audited_as_such(tmp_path, now):
    harness = make_api(tmp_path, now)
    harness.raw("POST", "/v1/propose", body=b"{}")
    row = harness.audit_rows()[-1]
    assert row["reason"] == "missing_header"
    assert row["data"]["outcome"] == "unauthorized"


# --- payload hygiene -------------------------------------------------------
def test_secrets_are_redacted_from_payloads(tmp_path, now):
    state = {"account": {"equity": 1.0, "api_secret": "leak", "broker_key": "leak"}}
    harness = make_api(tmp_path, now, state=lambda: state)
    response = harness.request("GET", "/v1/state/account", key="k-read")
    assert response.status == 200
    assert response.payload == {"account": {"equity": 1.0}}


# --- transport safety ------------------------------------------------------
def test_split_bind_accepts_loopback():
    assert _split_bind("127.0.0.1:8787") == ("127.0.0.1", 8787)
    assert _split_bind("localhost:8787") == ("localhost", 8787)
    assert _split_bind("[::1]:8787") == ("::1", 8787)


@pytest.mark.parametrize("bind", ["0.0.0.0:8787", "192.168.1.5:8787", "[::]:8787"])
def test_serve_refuses_to_bind_a_non_loopback_address(tmp_path, now, bind):
    harness = make_api(tmp_path, now, bind=bind)
    with pytest.raises(ValueError):
        harness.api.serve()
