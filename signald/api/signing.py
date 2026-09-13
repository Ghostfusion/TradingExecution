"""Signed control-API primitives: canonical string, HMAC, nonce store, roles.

Invariant (plan §7.2, design §12.4): every non-GET request is authenticated
against a role-scoped key by an HMAC over a canonical byte string, and a nonce
is consumed only **after** the signature matches -- a replayed or forged
request never reaches the handler and never burns a nonce. Failures carry a
machine reason for the audit ledger; the client is told nothing.

The module is stdlib-only (``hmac``/``hashlib``/``json``/``datetime``) and does
no I/O beyond the optional nonce journal; time is always injected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from signald.stores import _atomic_append

#: Closed role set, least to most privileged (plan §7.2).
ROLES = ("read", "propose", "execute", "admin")

KEY_ID_HEADER = "X-Key-Id"
TIMESTAMP_HEADER = "X-Timestamp"
NONCE_HEADER = "X-Nonce"
SIGNATURE_HEADER = "X-Signature"


def canonical_string(
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    key_id: str,
) -> str:
    """The exact byte string that is signed (plan §7.2).

    ``METHOD "\\n" PATH "\\n" SHA256_HEX(body) "\\n" timestamp "\\n" nonce
    "\\n" key_id`` -- no trailing newline, no normalisation beyond the body hash.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method, path, body_hash, timestamp, nonce, key_id])


def sign(secret: str, **parts: str) -> str:
    """Hex HMAC-SHA256 of the canonical string under ``secret`` (plan §7.2)."""
    message = canonical_string(**parts).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret: str, signature: str, **parts: str) -> bool:
    """Constant-time check of ``signature`` against the canonical string.

    ``hmac.compare_digest`` never short-circuits, so a prefix or truncated
    signature compares false in time independent of the correct value.
    """
    if not isinstance(signature, str):
        return False
    expected = sign(secret, **parts)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def _as_utc(value: datetime) -> datetime:
    """Normalise naive/aware datetimes to aware UTC (naive is assumed UTC)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_iso(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


class NonceStore:
    """Single-use nonces bounded by a TTL, optionally journalled to disk.

    ``consume`` returns True exactly once per nonce and False while the nonce is
    inside ``ttl_s``; an entry older than the TTL is forgotten, so the replay
    window (always shorter) is what actually caps reuse. ``seen`` peeks without
    consuming, which is what lets the authenticator reject a replay *before*
    spending an HMAC and consume only after the signature matches.
    """

    def __init__(
        self,
        path: str | Path | None,
        ttl_s: float = 600.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = None if path is None else Path(path)
        self.ttl_s = float(ttl_s)
        self._now = now or (lambda: datetime.now(UTC))
        self._seen: dict[str, datetime] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None or not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            nonce = row.get("nonce")
            at = _parse_iso(row.get("at"))
            if not nonce or at is None:
                continue
            at = _as_utc(at)
            prev = self._seen.get(nonce)
            if prev is None or at < prev:
                self._seen[nonce] = at

    def _purge(self, now: datetime) -> None:
        stale = [n for n, at in self._seen.items() if (now - at).total_seconds() >= self.ttl_s]
        for nonce in stale:
            del self._seen[nonce]

    def seen(self, nonce: str) -> bool:
        """True when ``nonce`` was already consumed inside the TTL (no effect)."""
        now = _as_utc(self._now())
        self._load()
        self._purge(now)
        return nonce in self._seen

    def consume(self, nonce: str) -> bool:
        """Claim ``nonce``; True on the first claim, False until the TTL lapses."""
        now = _as_utc(self._now())
        self._load()
        self._purge(now)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        if self.path is not None:
            row = json.dumps({"nonce": nonce, "at": now.isoformat()}, sort_keys=True)
            _atomic_append(self.path, row + "\n")
        return True


@dataclass(frozen=True)
class AuthResult:
    """The outcome of one authentication attempt; ``reason`` is audit-only."""

    ok: bool
    role: str | None
    reason: str


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return str(value).strip()
    return ""


def _role_allows(role: str, required: str) -> bool:
    """True when ``role`` is at least as privileged as ``required`` (fail closed)."""
    if role not in ROLES or required not in ROLES:
        return False
    return ROLES.index(role) >= ROLES.index(required)


class Authenticator:
    """Keyed HMAC authentication with replay window, nonce and role gates.

    Verify order (plan §7.2) is: timestamp window -> nonce unused -> constant-time
    HMAC -> consume the nonce. The signature must match before the nonce is spent,
    so an attacker cannot burn a legitimate client's nonce.
    """

    def __init__(
        self,
        keys: Mapping[str, str],
        roles: Mapping[str, str],
        *,
        replay_window_s: float = 300.0,
        nonce_store: NonceStore,
        now: Callable[[], datetime],
    ) -> None:
        self.keys = dict(keys)
        self.roles = dict(roles)
        self.replay_window_s = float(replay_window_s)
        self.nonce_store = nonce_store
        self._now = now

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        required_role: str | None = None,
    ) -> AuthResult:
        key_id = _header(headers, KEY_ID_HEADER)
        timestamp = _header(headers, TIMESTAMP_HEADER)
        nonce = _header(headers, NONCE_HEADER)
        signature = _header(headers, SIGNATURE_HEADER)
        if not (key_id and timestamp and nonce and signature):
            return AuthResult(False, None, "missing_header")

        secret = self.keys.get(key_id)
        if secret is None:
            return AuthResult(False, None, "unknown_key")
        role = self.roles.get(key_id)
        if role is None or role not in ROLES:
            return AuthResult(False, None, "forbidden")

        parsed = _parse_iso(timestamp)
        if parsed is None:
            return AuthResult(False, None, "stale_timestamp")
        skew = abs((_as_utc(self._now()) - _as_utc(parsed)).total_seconds())
        if skew > self.replay_window_s / 2.0:
            return AuthResult(False, None, "stale_timestamp")

        if self.nonce_store.seen(nonce):
            return AuthResult(False, None, "replayed_nonce")

        ok = verify(
            secret,
            signature,
            method=method,
            path=path,
            body=body,
            timestamp=timestamp,
            nonce=nonce,
            key_id=key_id,
        )
        if not ok:
            return AuthResult(False, None, "bad_signature")

        if not self.nonce_store.consume(nonce):
            return AuthResult(False, None, "replayed_nonce")

        if required_role is not None and not _role_allows(role, required_role):
            return AuthResult(False, role, "forbidden")
        return AuthResult(True, role, "ok")
