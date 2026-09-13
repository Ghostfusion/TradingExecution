"""Signed control API over the risk/order core (plan §7.1, design §12.4).

Invariant: deny by default. Every request is authenticated against a keyed role
before a route is reached; a bad key, a stale timestamp, a replay, a forged
signature and an under-privileged role all return a generic ``401``/``403``
with ``{"error": "unauthorized"}`` while the audit ledger records the *specific*
reason. The API is off unless explicitly enabled, binds loopback only, and its
payload never carries a secret or a broker key. ``handle`` is a pure function of
its ``Request`` (no sockets); ``serve`` is the thin ``http.server`` wrapper used
in production and is never exercised with sockets by the tests.
"""

from __future__ import annotations

import functools
import http.server
import ipaddress
import json
import re
import socket
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from signald.api.signing import Authenticator, AuthResult
from signald.risk.state import SLEEVES
from signald.stores import AuditChain

#: Request bodies larger than this are refused before authentication (plan §7.2).
MAX_BODY_BYTES = 1024 * 1024

#: Endpoint table: (method, path) -> (required role, handler name).
_READ_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/v1/state/account"): "account",
    ("GET", "/v1/state/positions"): "positions",
    ("GET", "/v1/state/risk"): "risk",
    ("GET", "/v1/state/sleeves"): "sleeves",
    ("GET", "/v1/signals/latest"): "signal",
    ("GET", "/v1/orders/open"): "orders",
}
_MUTATING_ROUTES: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/v1/propose"): ("propose", "propose"),
    ("POST", "/v1/simulate"): ("propose", "simulate"),
    ("POST", "/v1/orders/submit"): ("execute", "submit"),
    ("POST", "/v1/orders/cancel"): ("execute", "cancel"),
    ("POST", "/v1/halt"): ("execute", "halt"),
}
_CEILING_RE = re.compile(r"^/v1/sleeves/(?P<sleeve>[A-Za-z]+)/ceiling$")

#: Keys whose value must never leave the process (plan §7.2, §12.4).
_SECRET_KEY_RE = re.compile(r"secret|password|signing|^key$|_key$")


@dataclass(frozen=True)
class Request:
    """One decoded HTTP request, transport-independent."""

    method: str
    path: str
    query: Mapping[str, str]
    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class Response:
    """One response: a status and a JSON-serialisable payload."""

    status: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Route:
    name: str
    role: str
    sleeve: str | None = None


def _route(method: str, path: str) -> _Route | None:
    """Resolve an endpoint; anything not in the table is a 404 (fail closed)."""
    read = _READ_ROUTES.get((method, path))
    if read is not None:
        return _Route(read, "read")
    mutating = _MUTATING_ROUTES.get((method, path))
    if mutating is not None:
        role, name = mutating
        return _Route(name, role)
    if method == "POST":
        match = _CEILING_RE.match(path)
        if match is not None and match.group("sleeve") in SLEEVES:
            return _Route("ceiling", "admin", match.group("sleeve"))
    return None


def _redact(value: Any) -> Any:
    """Recursively drop secret-shaped keys from an outbound payload."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key).lower()):
                continue
            out[str(key)] = _redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _parse_body(body: bytes) -> dict[str, Any] | None:
    """Decode a JSON object body; anything else is a bad request."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _split_bind(bind: str) -> tuple[str, int]:
    """Split ``host:port`` and refuse a non-loopback host (plan §7.2)."""
    host, sep, port_text = bind.rpartition(":")
    if not sep or not host or not port_text:
        raise ValueError(f"bind must be host:port, got {bind!r}")
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"bind port is not a number: {port_text!r}") from exc
    if not _is_loopback(host):
        raise ValueError(f"control API refuses non-loopback bind: {bind!r}")
    return host, port


class ControlApi:
    """The signed control surface (plan §7.1).

    All I/O is injected. ``state`` returns the read snapshot keyed by resource
    (``account``/``positions``/``risk``/``sleeves``/``signals``/``orders``); the
    mutation callables receive the decoded body and return a payload dict.
    """

    def __init__(
        self,
        *,
        authenticator: Authenticator,
        state: Callable[[], Mapping[str, Any]],
        simulate: Callable[[dict], dict] | None = None,
        propose: Callable[[dict], dict] | None = None,
        submit: Callable[[dict], dict] | None = None,
        cancel: Callable[[dict], dict] | None = None,
        halt: Callable[[], dict] | None = None,
        ceiling: Callable[[str, dict], dict] | None = None,
        audit: AuditChain | None = None,
        enabled: bool = False,
        bind: str = "127.0.0.1:8787",
    ) -> None:
        self.authenticator = authenticator
        self.state = state
        self.simulate = simulate
        self.propose = propose
        self.submit = submit
        self.cancel = cancel
        self.halt = halt
        self.ceiling = ceiling
        self.audit = audit
        self.enabled = enabled
        self.bind = bind

    # -- request entry ---------------------------------------------------
    def handle(self, request: Request) -> Response:
        """Authenticate, route and dispatch one request; never raises."""
        if not self.enabled:
            return self._finish(request, None, 503, {"error": "api_disabled"},
                                "api_disabled")
        if len(request.body) > MAX_BODY_BYTES:
            return self._finish(request, None, 413, {"error": "payload_too_large"},
                                "payload_too_large")

        route = _route(request.method, request.path)
        required = None if route is None or route.name == "halt" else route.role
        auth = self.authenticator.authenticate(
            method=request.method,
            path=request.path,
            body=request.body,
            headers=request.headers,
            required_role=required,
        )
        if not auth.ok:
            status = 403 if auth.reason == "forbidden" else 401
            return self._finish(request, auth, status, {"error": "unauthorized"},
                                "unauthorized")
        if route is None:
            return self._finish(request, auth, 404, {"error": "not_found"}, "not_found")

        try:
            return self._dispatch(request, route, auth)
        except Exception:  # a handler fault is a 500, never a leak to the client
            return self._finish(request, auth, 500, {"error": "internal_error"},
                                "internal_error")

    def _dispatch(self, request: Request, route: _Route, auth: AuthResult) -> Response:
        if route.name in _READ_ROUTES.values():
            return self._read(request, route, auth)
        if route.name == "halt":
            if self.halt is None:
                return self._finish(request, auth, 503, {"error": "api_disabled"},
                                    "api_disabled")
            return self._ok(request, auth, self.halt())
        return self._mutate(request, route, auth)

    def _read(self, request: Request, route: _Route, auth: AuthResult) -> Response:
        snapshot = self.state()
        if route.name == "signal":
            symbol = request.query.get("symbol") or ""
            if not symbol:
                return self._finish(request, auth, 400, {"error": "missing_symbol"},
                                    "missing_symbol")
            signals = snapshot.get("signals", {}) if isinstance(snapshot, Mapping) else {}
            value = signals.get(symbol) if isinstance(signals, Mapping) else None
            return self._ok(request, auth, {"symbol": symbol, "signal": value})
        value = snapshot.get(route.name) if isinstance(snapshot, Mapping) else None
        return self._ok(request, auth, {route.name: value})

    def _mutate(self, request: Request, route: _Route, auth: AuthResult) -> Response:
        handler = {
            "propose": self.propose,
            "simulate": self.simulate,
            "submit": self.submit,
            "cancel": self.cancel,
            "ceiling": self.ceiling,
        }.get(route.name)
        if handler is None:
            return self._finish(request, auth, 503, {"error": "api_disabled"},
                                "api_disabled")
        body = _parse_body(request.body)
        if body is None:
            return self._finish(request, auth, 400, {"error": "bad_request"},
                                "bad_request")
        if route.name == "submit" and not (body.get("proposal_id") and body.get("approval_id")):
            return self._finish(request, auth, 400, {"error": "approval_required"},
                                "approval_required")
        if route.name == "ceiling":
            result = self.ceiling(route.sleeve or "", body)
        else:
            result = handler(body)
        return self._ok(request, auth, result)

    # -- response helpers ------------------------------------------------
    def _ok(self, request: Request, auth: AuthResult, payload: Mapping[str, Any]) -> Response:
        redacted = _redact(dict(payload))
        return self._finish(request, auth, 200, redacted, "ok")

    def _finish(
        self,
        request: Request,
        auth: AuthResult | None,
        status: int,
        payload: dict[str, Any],
        outcome: str,
        reason: str | None = None,
    ) -> Response:
        if self.audit is not None:
            self.audit.append(
                "api_request",
                reason or (auth.reason if auth is not None else outcome),
                role=auth.role if auth is not None else None,
                method=request.method,
                path=request.path,
                outcome=outcome,
                status=status,
            )
        return Response(status, payload)

    # -- production transport (never exercised with sockets in tests) -----
    def serve(self) -> None:
        """Bind loopback and serve; a non-loopback bind is refused outright."""
        host, port = _split_bind(self.bind)
        handler = functools.partial(_ControlHandler, api=self)
        server = _http_server(host, port, handler)
        with server as httpd:
            httpd.serve_forever()


def _http_server(host: str, port: int, handler: Any) -> http.server.ThreadingHTTPServer:
    """A threaded HTTP server whose address family follows the loopback host."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_cls = type(
        "_LoopbackServer", (http.server.ThreadingHTTPServer,), {"address_family": family}
    )
    return server_cls((host, port), handler)


class _ControlHandler(http.server.BaseHTTPRequestHandler):
    """Thin ``http.server`` adapter around :meth:`ControlApi.handle`."""

    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, api: ControlApi, **kwargs: Any) -> None:
        self._api = api
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        parsed = urllib.parse.urlsplit(self.path)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        request = Request(
            method=self.command,
            path=parsed.path,
            query=query,
            body=body,
            headers=dict(self.headers),
        )
        response = self._api.handle(request)
        data = json.dumps(response.payload, sort_keys=True, default=str).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log; the audit ledger is the log."""
