"""P5 signing primitives: canonical string, HMAC, nonce store, roles (plan §7.2).

Every assertion here is on observable behaviour: the exact signed bytes, a
constant-time comparison call, the replay window, single-use nonces, and the
machine reason each denial carries into the audit ledger.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import pytest

from signald.api.signing import (
    Authenticator,
    NonceStore,
    canonical_string,
    sign,
    verify,
)

pytestmark = pytest.mark.timeout(120)

SECRET = "topsecret"
#: A fixed request used across the hand-worked examples.
PARTS = {
    "method": "POST",
    "path": "/v1/propose",
    "body": b'{"a":1}',
    "timestamp": "2026-09-12T12:00:00+00:00",
    "nonce": "abc123",
    "key_id": "k1",
}


# --- canonical string ------------------------------------------------------
def test_canonical_string_is_byte_exact():
    # SHA256(b'{"a":1}') = 015abd7f...f862, hand-derived, and the join is six
    # newline-separated fields with no trailing newline.
    expected = (
        "POST\n/v1/propose\n"
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862\n"
        "2026-09-12T12:00:00+00:00\nabc123\nk1"
    )
    assert canonical_string(**PARTS) == expected
    assert canonical_string(**PARTS).count("\n") == 5


def test_canonical_string_hashes_the_body_not_echoes_it():
    # Two bodies with the same length but different content hash differently.
    a = canonical_string(**{**PARTS, "body": b"AAAA"})
    b = canonical_string(**{**PARTS, "body": b"BBBB"})
    assert a.split("\n")[2] == hashlib.sha256(b"AAAA").hexdigest()
    assert a != b


# --- sign / verify ---------------------------------------------------------
def test_sign_is_the_hand_worked_hmac():
    assert sign(SECRET, **PARTS) == (
        "37865a113287f9eeeb4112eb9a2621adc23c9372dae9f5394deaab31ed0339aa"
    )


def test_a_valid_signature_verifies():
    assert verify(SECRET, sign(SECRET, **PARTS), **PARTS) is True


def test_a_single_byte_body_change_fails():
    signature = sign(SECRET, **PARTS)
    assert verify(SECRET, signature, **{**PARTS, "body": b'{"a":2}'}) is False


def test_the_wrong_secret_fails():
    assert verify("other", sign(SECRET, **PARTS), **PARTS) is False


def test_verify_uses_constant_time_compare(monkeypatch):
    calls: list[tuple] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert verify(SECRET, sign(SECRET, **PARTS), **PARTS) is True
    assert calls, "verify must compare through hmac.compare_digest"


@pytest.mark.parametrize("mutate", [
    lambda s: s[: len(s) // 2],   # truncated
    lambda s: s[:-1],             # one hex digit short
    lambda s: "0" + s,            # prefixed
    lambda s: s.upper(),          # correct value, wrong case
    lambda s: "",                 # empty
])
def test_prefix_and_truncated_signatures_are_rejected(mutate):
    signature = sign(SECRET, **PARTS)
    assert verify(SECRET, mutate(signature), **PARTS) is False


# --- nonce store -----------------------------------------------------------
def test_a_nonce_is_single_use(tmp_path):
    store = NonceStore(tmp_path / "nonces.jsonl")
    assert store.consume("n1") is True
    assert store.consume("n1") is False


def test_a_nonce_expires_after_the_ttl(now):
    clock = [now]
    store = NonceStore(None, ttl_s=600.0, now=lambda: clock[0])
    assert store.consume("n1") is True
    clock[0] = now + timedelta(seconds=599)
    assert store.consume("n1") is False
    clock[0] = now + timedelta(seconds=600)
    assert store.consume("n1") is True


def test_nonce_store_persists_across_instances(tmp_path, now):
    path = tmp_path / "nonces.jsonl"
    assert NonceStore(path, now=lambda: now).consume("n1") is True
    assert NonceStore(path, now=lambda: now).consume("n1") is False


def test_seen_peeks_without_consuming(now):
    store = NonceStore(None, now=lambda: now)
    assert store.seen("n1") is False
    assert store.consume("n1") is True
    assert store.seen("n1") is True


# --- authenticator ---------------------------------------------------------
def _headers(*, secret: str = SECRET, key_id: str = "k1", nonce: str = "n1",
             timestamp: str, body: bytes = b"{}", path: str = "/v1/propose") -> dict[str, str]:
    signature = sign(
        secret, method="POST", path=path, body=body, timestamp=timestamp,
        nonce=nonce, key_id=key_id,
    )
    return {
        "X-Key-Id": key_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


def _auth(now, tmp_path, *, roles=None, window=300.0):
    store = NonceStore(tmp_path / "nonces.jsonl", now=lambda: now)
    return Authenticator(
        {"k1": SECRET},
        roles if roles is not None else {"k1": "execute"},
        replay_window_s=window,
        nonce_store=store,
        now=lambda: now,
    )


def test_authenticate_accepts_a_fresh_valid_request(now, tmp_path):
    auth = _auth(now, tmp_path)
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=now.isoformat()),
    )
    assert result.ok is True
    assert result.role == "execute"
    assert result.reason == "ok"


def test_replay_window_accepts_inside_and_rejects_outside(now, tmp_path):
    auth = _auth(now, tmp_path)
    inside = (now - timedelta(seconds=149)).isoformat()
    outside = (now - timedelta(seconds=151)).isoformat()
    good = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=inside, nonce="in"),
    )
    assert good.ok is True
    stale = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=outside, nonce="out"),
    )
    assert stale.ok is False
    assert stale.reason == "stale_timestamp"


def test_a_malformed_timestamp_is_stale(now, tmp_path):
    auth = _auth(now, tmp_path)
    headers = _headers(timestamp=now.isoformat())
    headers["X-Timestamp"] = "not-a-time"
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}", headers=headers,
    )
    assert result.reason == "stale_timestamp"


def test_a_replayed_nonce_is_rejected(now, tmp_path):
    auth = _auth(now, tmp_path)
    headers = _headers(timestamp=now.isoformat(), nonce="once")
    assert auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}", headers=headers,
    ).ok
    replayed = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}", headers=headers,
    )
    assert replayed.ok is False
    assert replayed.reason == "replayed_nonce"


def test_a_bad_signature_does_not_burn_the_nonce(now, tmp_path):
    auth = _auth(now, tmp_path)
    tampered = _headers(timestamp=now.isoformat(), nonce="keep")
    tampered["X-Signature"] = "0" * 64
    bad = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}", headers=tampered,
    )
    assert bad.reason == "bad_signature"
    # The same nonce still works: it was not consumed by the failed attempt.
    good = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=now.isoformat(), nonce="keep"),
    )
    assert good.ok is True


def test_an_unknown_key_id_is_denied(now, tmp_path):
    auth = _auth(now, tmp_path)
    headers = _headers(timestamp=now.isoformat(), key_id="ghost")
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}", headers=headers,
    )
    assert result.ok is False
    assert result.reason == "unknown_key"
    assert result.role is None


@pytest.mark.parametrize("missing", ["X-Key-Id", "X-Timestamp", "X-Nonce", "X-Signature"])
def test_a_missing_header_is_denied_with_its_code(now, tmp_path, missing):
    auth = _auth(now, tmp_path)
    headers = _headers(timestamp=now.isoformat())
    del headers[missing]
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}", headers=headers,
    )
    assert result.ok is False
    assert result.reason == "missing_header"


def test_role_escalation_is_denied(now, tmp_path):
    auth = _auth(now, tmp_path, roles={"k1": "read"})
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=now.isoformat()), required_role="execute",
    )
    assert result.ok is False
    assert result.reason == "forbidden"


def test_a_higher_role_satisfies_a_lower_requirement(now, tmp_path):
    auth = _auth(now, tmp_path, roles={"k1": "admin"})
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=now.isoformat()), required_role="read",
    )
    assert result.ok is True
    assert result.role == "admin"


def test_an_unknown_required_role_fails_closed(now, tmp_path):
    auth = _auth(now, tmp_path, roles={"k1": "read"})
    result = auth.authenticate(
        method="POST", path="/v1/propose", body=b"{}",
        headers=_headers(timestamp=now.isoformat()), required_role="root",
    )
    assert result.ok is False
    assert result.reason == "forbidden"
