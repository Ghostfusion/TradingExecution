"""MCP server: least-privilege tool dispatch over a hash-pinned approval store
(plan §7.3-7.4, design §12.2-12.3).

Invariant: tool annotations are untrusted hints and the server-side allow-list
(``tools_for``) is the control. A mutating tool never runs without a matching,
single-use, unexpired human approval bound to the proposal hash; an LLM payload
never carries a number that sizes, prices or gates a trade (design D6); and the
kill switch (``halt_trading``) is never gated. Every call appends exactly one
audit row carrying no free text from the model, and every outbound payload is
redacted of secret-shaped keys. Transports (sockets) live only in :meth:`serve`.
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
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from signald.schema import sha256_of
from signald.stores import AuditChain, _atomic_append

from .tools import (
    TOOL_TABLE,
    ToolSpec,
    assert_descriptions_unchanged,
    refuses_llm_numbers,
    tools_for,
)

#: JSON-RPC / MCP framing constants (plan §7.3).
_JSONRPC_VERSION = "2.0"
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "signald-mcp"
_SERVER_VERSION = "1.0.0"

#: Keys whose value must never leave the process (design §12.3).
_SECRET_KEY_RE = re.compile(r"secret|password|signing|^key$|_key$")

#: Roles whose approval authorises an operator-only config change.
OPERATOR_ROLES: tuple[str, ...] = ("operator", "admin")

#: Outcomes that are not an error from the caller's point of view.
_OK_OUTCOMES: frozenset[str] = frozenset(
    {"ok", "created", "submitted", "cancelled", "halted", "configured"}
)

_TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_TABLE}


class ProposalRefused(ValueError):
    """An intent refused before any effect (fail closed). Carries the gate verdict."""

    def __init__(self, code: str, detail: str, *, verdict: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.verdict = dict(verdict) if verdict is not None else None


@dataclass(frozen=True)
class Proposal:
    """A pending, immutable, hash-pinned proposal (plan §2.4)."""

    proposal_id: str
    payload_hash: str
    payload: dict[str, Any]
    created_by: str  # "llm" | "human" | "sleeve"
    created_at: str
    state: str  # "pending" | "approved" | "rejected" | "expired"


@dataclass(frozen=True)
class Approval:
    """A single-use human approval bound to one proposal hash (plan §2.4)."""

    approval_id: str
    proposal_id: str
    payload_hash: str
    approved_by: str
    approved_at: str
    expires_at: str
    single_use: bool = True


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


def _parse_ts(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def _validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> str | None:
    """Enforce the required keys and declared types of ``spec.input_schema``."""
    properties = spec.input_schema.get("properties") or {}
    for key in spec.input_schema.get("required") or ():
        if key not in arguments:
            return f"missing required argument: {key}"
    for key, value in arguments.items():
        declared = properties.get(key)
        if not isinstance(declared, Mapping):
            continue
        want = declared.get("type")
        if want == "string" and not isinstance(value, str):
            return f"{key} must be a string"
        if want == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"{key} must be a number"
        if want == "object" and not isinstance(value, Mapping):
            return f"{key} must be an object"
        if want == "array" and not isinstance(value, (list, tuple)):
            return f"{key} must be an array"
    return None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_bind(bind: str) -> tuple[str, int]:
    """Split ``host:port`` and refuse a non-loopback host (plan §7.3)."""
    host, sep, port_text = str(bind).rpartition(":")
    if not sep:
        host, port_text = port_text, "8787"
    host = host or "127.0.0.1"
    if not _is_loopback(host):
        raise ValueError(f"MCP must bind loopback, not {host!r}")
    return host, int(port_text)


class McpServer:
    """The MCP surface: tool table, proposal store and approval binding.

    Callbacks are the effectful seams (all optional): ``state`` supplies the read
    snapshot, ``simulate`` is the house gate, ``submit``/``cancel``/``halt``/
    ``set_allocation`` are the order-path effects, and ``propose`` is an optional
    host hook called with a freshly created proposal. The server itself owns the
    policy: the allow-list, the LLM-number refusal, the BLOCK refusal and the
    single-use approval check all run here and cannot be bypassed by a host.
    """

    def __init__(
        self,
        *,
        toolsets: str = "read,simulate,propose",
        state: Callable[[], Mapping[str, Any]] | None = None,
        simulate: Callable[[dict], dict] | None = None,
        propose: Callable[[dict], dict] | None = None,
        submit: Callable[[dict], dict] | None = None,
        cancel: Callable[[dict], dict] | None = None,
        halt: Callable[[], dict] | None = None,
        set_allocation: Callable[[dict], dict] | None = None,
        proposals_path: str | Path | None = None,
        approvals_path: str | Path | None = None,
        audit: AuditChain | None = None,
        now: Callable[[], datetime] | None = None,
        approval_ttl_s: float = 900.0,
        pinned_descriptions: Mapping[str, str] | None = None,
    ) -> None:
        # An unknown toolset name raises here (fail closed before serving).
        self._allowed = tools_for(toolsets)
        self._allowed_names = frozenset(spec.name for spec in self._allowed)
        self._toolsets = toolsets
        self._state = state
        self._simulate = simulate
        self._propose = propose
        self._submit = submit
        self._cancel = cancel
        self._halt = halt
        self._set_allocation = set_allocation
        self._proposals_path = Path(proposals_path) if proposals_path is not None else None
        self._approvals_path = Path(approvals_path) if approvals_path is not None else None
        self._audit = audit
        self._now_fn = now or datetime.now
        self._ttl = float(approval_ttl_s)
        self._proposals: dict[str, Proposal] = {}
        self._approvals: dict[str, Approval] = {}
        self._consumed: set[str] = set()
        if pinned_descriptions is not None:
            assert_descriptions_unchanged(pinned_descriptions)
        self._load_proposals()
        self._load_approvals()

    # -- MCP surface -----------------------------------------------------
    def list_tools(self) -> list[dict]:
        """The MCP ``tools/list`` payload for the allowed toolsets; never a credential."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "annotations": {
                    "readOnlyHint": spec.read_only,
                    "destructiveHint": spec.destructive,
                    "idempotentHint": spec.idempotent,
                    "openWorldHint": spec.open_world,
                },
            }
            for spec in self._allowed
        ]

    def call(self, name: str, arguments: dict) -> dict:
        """Dispatch one tool call; never raises, always audits exactly once."""
        spec = _TOOLS_BY_NAME.get(str(name))
        if spec is None or (
            spec.name not in self._allowed_names and spec.class_ != "safety"
        ):
            return self._record(
                tool=str(name), toolset=None, outcome="not_found",
                payload={"error": "tool_not_found", "tool": str(name)},
            )
        if not isinstance(arguments, Mapping):
            return self._record(
                tool=spec.name, toolset=spec.toolset, outcome="refused",
                payload={"error": "arguments_must_be_object"},
            )
        invalid = _validate_arguments(spec, arguments)
        if invalid is not None:
            return self._record(
                tool=spec.name, toolset=spec.toolset, outcome="refused",
                payload={"error": "invalid_arguments", "detail": invalid},
            )
        handler = getattr(self, f"_tool_{spec.name}")
        try:
            outcome, payload, proposal_id = handler(dict(arguments))
        except ProposalRefused as exc:
            payload = {"error": exc.code, "detail": exc.detail}
            if exc.verdict is not None:
                payload["verdict"] = exc.verdict
            outcome, proposal_id = "blocked" if exc.code == "blocked" else "refused", None
        except Exception as exc:  # a handler fault is a refusal, never a pass
            outcome, payload = "error", {"error": "handler_error", "detail": type(exc).__name__}
            proposal_id = None
        return self._record(
            tool=spec.name, toolset=spec.toolset, outcome=outcome,
            payload=payload, proposal_id=proposal_id,
        )

    def handle_message(self, message: dict) -> dict:
        """JSON-RPC 2.0 framing over an in-memory dict (initialize/list/call)."""
        if not isinstance(message, Mapping):
            return _rpc_error(None, -32600, "invalid request")
        mid = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return _rpc_error(mid, -32600, "invalid request")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            }
        elif method == "tools/list":
            result = {"tools": self.list_tools()}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name") if isinstance(params, Mapping) else None
            args = params.get("arguments") if isinstance(params, Mapping) else None
            if not isinstance(name, str):
                return _rpc_error(mid, -32602, "invalid params")
            result = self.call(name, args if args is not None else {})
        elif method in ("notifications/initialized", "initialized"):
            return {}
        else:
            return _rpc_error(mid, -32601, "method not found")
        if mid is None:
            return {}
        return {"jsonrpc": _JSONRPC_VERSION, "id": mid, "result": result}

    # -- proposal lifecycle ----------------------------------------------
    def propose_order(self, payload: dict) -> Proposal:
        """Create an immutable pending proposal, or refuse (plan §7.4).

        Refuses an LLM payload carrying a numeric trading field and refuses to
        create anything when the gate verdict is ``BLOCK``; the refusal carries
        the verdict and its binding gate.
        """
        clean = json.loads(json.dumps(dict(payload), default=str))
        reason = refuses_llm_numbers(clean)
        if reason is not None:
            raise ProposalRefused(
                "llm_number", f"LLM payload carries numeric trading field: {reason}"
            )
        if self._simulate is None:
            raise ProposalRefused("simulate_unavailable", "no simulate callback")
        verdict = self._simulate(clean)
        if not isinstance(verdict, Mapping):
            raise ProposalRefused("simulate_unavailable", "simulate returned no verdict")
        state = str(verdict.get("verdict") or "").upper()
        if state == "BLOCK":
            raise ProposalRefused("blocked", "gate verdict is BLOCK", verdict=verdict)
        if state not in {"ALLOW", "REDUCE"}:
            raise ProposalRefused(
                "simulate_unavailable", f"unknown gate verdict: {state or 'none'}"
            )
        created_at = self._now_fn().isoformat(timespec="seconds")
        payload_hash = sha256_of(clean)
        pin = {"payload_hash": payload_hash, "created_at": created_at}
        proposal_id = "prop_" + sha256_of(pin)[:16]
        proposal = Proposal(
            proposal_id=proposal_id,
            payload_hash=payload_hash,
            payload=clean,
            created_by="llm",
            created_at=created_at,
            state="pending",
        )
        self._store_proposal(proposal)
        return proposal

    def approve(self, proposal_id: str, *, approved_by: str) -> Approval:
        """Issue a single-use approval bound to a stored proposal's hash."""
        proposal = self._proposals.get(str(proposal_id))
        if proposal is None or not _payload_intact(proposal):
            raise ValueError(f"unknown proposal: {proposal_id!r}")
        now = self._now_fn()
        approved_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(seconds=self._ttl)).isoformat(timespec="seconds")
        binding = {
            "proposal_id": proposal.proposal_id,
            "approved_by": approved_by,
            "approved_at": approved_at,
        }
        approval_id = "appr_" + sha256_of(binding)[:16]
        approval = Approval(
            approval_id=approval_id,
            proposal_id=proposal.proposal_id,
            payload_hash=proposal.payload_hash,
            approved_by=str(approved_by),
            approved_at=approved_at,
            expires_at=expires_at,
            single_use=True,
        )
        self._approvals.setdefault(approval_id, approval)
        self._persist_approval(approval)
        return self._approvals[approval_id]

    # -- tool handlers ---------------------------------------------------
    def _tool_get_account_state(self, args: dict) -> tuple[str, dict, str | None]:
        return self._read("account")

    def _tool_get_positions(self, args: dict) -> tuple[str, dict, str | None]:
        return self._read("positions")

    def _tool_get_open_orders(self, args: dict) -> tuple[str, dict, str | None]:
        return self._read("orders")

    def _tool_get_risk_state(self, args: dict) -> tuple[str, dict, str | None]:
        return self._read("risk")

    def _tool_get_sleeve_allocation(self, args: dict) -> tuple[str, dict, str | None]:
        return self._read("sleeves")

    def _read(self, key: str) -> tuple[str, dict, str | None]:
        snapshot = self._snapshot()
        if snapshot is None:
            return "unavailable", {"error": "state_unavailable"}, None
        value = snapshot.get(key)
        if value is None:
            return "unavailable", {"error": f"{key}_unavailable"}, None
        return "ok", {key: value}, None

    def _tool_get_signal_latest(self, args: dict) -> tuple[str, dict, str | None]:
        symbol = str(args.get("symbol", "")).strip()
        snapshot = self._snapshot()
        signals = snapshot.get("signals") if snapshot is not None else None
        if not isinstance(signals, Mapping):
            return "unavailable", {"error": "signals_unavailable"}, None
        value = signals.get(symbol)
        if value is None:
            return "unavailable", {"error": "signal_unavailable"}, None
        return "ok", {"symbol": symbol, "signal": value}, None

    def _tool_simulate_order(self, args: dict) -> tuple[str, dict, str | None]:
        reason = refuses_llm_numbers(args)
        if reason is not None:
            return "refused", {"error": "llm_number", "detail": reason}, None
        if self._simulate is None:
            return "unavailable", {"error": "simulate_unavailable"}, None
        verdict = self._simulate(dict(args))
        if not isinstance(verdict, Mapping):
            return "unavailable", {"error": "simulate_unavailable"}, None
        return "ok", {"simulation": dict(verdict)}, None

    def _tool_propose_order(self, args: dict) -> tuple[str, dict, str | None]:
        proposal = self.propose_order(dict(args))
        payload: dict[str, Any] = {"proposal": asdict(proposal)}
        if self._propose is not None:
            extra = self._propose(asdict(proposal))
            if isinstance(extra, Mapping):
                payload["request"] = dict(extra)
        return "created", payload, proposal.proposal_id

    def _tool_submit_order(self, args: dict) -> tuple[str, dict, str | None]:
        if self._submit is None:
            return "unavailable", {"error": "submit_unavailable"}, None
        proposal_id = str(args.get("proposal_id") or "")
        approval_id = str(args.get("approval_id") or "")
        self._consume_approval(proposal_id=proposal_id, approval_id=approval_id)
        result = self._submit({"proposal_id": proposal_id, "approval_id": approval_id})
        return "submitted", {"result": _as_dict(result)}, proposal_id

    def _tool_cancel_order(self, args: dict) -> tuple[str, dict, str | None]:
        if self._cancel is None:
            return "unavailable", {"error": "cancel_unavailable"}, None
        approval_id = str(args.get("approval_id") or "")
        self._consume_approval(proposal_id=None, approval_id=approval_id)
        result = self._cancel(
            {"order_id": str(args.get("order_id") or ""), "approval_id": approval_id}
        )
        return "cancelled", {"result": _as_dict(result)}, None

    def _tool_halt_trading(self, args: dict) -> tuple[str, dict, str | None]:
        # The kill switch is never gated: no approval, and allowed even when the
        # `safety` toolset was not listed (plan §7.3, design §12.3).
        if self._halt is None:
            return "unavailable", {"error": "halt_unavailable"}, None
        return "halted", {"result": _as_dict(self._halt())}, None

    def _tool_set_sleeve_allocation(self, args: dict) -> tuple[str, dict, str | None]:
        if self._set_allocation is None:
            return "unavailable", {"error": "allocation_unavailable"}, None
        approval_id = str(args.get("approval_id") or "")
        self._consume_approval(
            proposal_id=None, approval_id=approval_id, require_operator=True
        )
        result = self._set_allocation(
            {"sleeve": str(args.get("sleeve") or ""), "capital_pct": args.get("capital_pct")}
        )
        return "configured", {"result": _as_dict(result)}, None

    # -- policy helpers --------------------------------------------------
    def _snapshot(self) -> Mapping[str, Any] | None:
        if self._state is None:
            return None
        snapshot = self._state()
        return snapshot if isinstance(snapshot, Mapping) else None

    def _consume_approval(
        self, *, proposal_id: str | None, approval_id: str, require_operator: bool = False
    ) -> Approval:
        """Validate and burn one approval; a repeat use or a hash mismatch refuses."""
        if not approval_id:
            raise ProposalRefused("approval_required", "approval_id is required")
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ProposalRefused("approval_unknown", "approval not found")
        if approval_id in self._consumed:
            raise ProposalRefused("approval_used", "approval already used")
        expires = _parse_ts(approval.expires_at)
        if expires is None or expires <= self._now_fn():
            raise ProposalRefused("approval_expired", "approval expired")
        if require_operator and approval.approved_by not in OPERATOR_ROLES:
            raise ProposalRefused("approval_operator", "operator approval required")
        if proposal_id is not None:
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not _payload_intact(proposal):
                raise ProposalRefused("proposal_unknown", "proposal not found")
            pinned = approval.payload_hash == proposal.payload_hash
            if approval.proposal_id != proposal_id or not pinned:
                raise ProposalRefused("hash_mismatch", "approval is not bound to this proposal")
        self._consumed.add(approval_id)
        self._persist_consumed(approval_id)
        return approval

    def _record(
        self,
        *,
        tool: str,
        toolset: str | None,
        outcome: str,
        payload: Mapping[str, Any],
        proposal_id: str | None = None,
    ) -> dict:
        """One audit row per call (no model free text) + one redacted MCP result."""
        if self._audit is not None:
            self._audit.append(
                "mcp",
                outcome,
                tool=tool,
                toolset=toolset,
                outcome=outcome,
                proposal_id=proposal_id,
            )
        body = json.dumps(_redact(dict(payload)), sort_keys=True, default=str)
        return {
            "content": [{"type": "text", "text": body}],
            "isError": outcome not in _OK_OUTCOMES,
        }

    # -- durable store ---------------------------------------------------
    def _store_proposal(self, proposal: Proposal) -> None:
        self._proposals.setdefault(proposal.proposal_id, proposal)
        if self._proposals_path is not None:
            line = json.dumps(asdict(proposal), sort_keys=True, default=str)
            _atomic_append(self._proposals_path, line + "\n")

    def _persist_approval(self, approval: Approval) -> None:
        if self._approvals_path is not None:
            row = {"type": "approval", **asdict(approval)}
            _atomic_append(self._approvals_path, json.dumps(row, sort_keys=True) + "\n")

    def _persist_consumed(self, approval_id: str) -> None:
        if self._approvals_path is not None:
            row = {"type": "consumed", "approval_id": approval_id}
            _atomic_append(self._approvals_path, json.dumps(row, sort_keys=True) + "\n")

    def _load_proposals(self) -> None:
        for row in _read_rows(self._proposals_path):
            proposal = _build(Proposal, row)
            if proposal is None or not _payload_intact(proposal):
                continue  # a tampered row is not a proposal (fail closed)
            self._proposals.setdefault(proposal.proposal_id, proposal)

    def _load_approvals(self) -> None:
        for row in _read_rows(self._approvals_path):
            if row.get("type") == "consumed":
                self._consumed.add(str(row.get("approval_id")))
            elif row.get("type") == "approval":
                approval = _build(Approval, row)
                if approval is not None:
                    self._approvals.setdefault(approval.approval_id, approval)

    # -- production transport (never exercised with sockets in tests) -----
    def serve(self, bind: str = "127.0.0.1:8787") -> None:
        """Bind loopback and serve JSON-RPC over HTTP; a non-loopback bind is refused."""
        host, port = _split_bind(bind)
        handler = functools.partial(_McpHandler, server=self)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        server_cls = type(
            "_LoopbackServer", (http.server.ThreadingHTTPServer,), {"address_family": family}
        )
        with server_cls((host, port), handler) as httpd:
            httpd.serve_forever()


def _as_dict(value: Any) -> Any:
    return dict(value) if isinstance(value, Mapping) else value


def _payload_intact(proposal: Proposal) -> bool:
    """A proposal is only usable while its payload still hashes to its pin."""
    if not isinstance(proposal.payload, dict):
        return False
    return sha256_of(proposal.payload) == proposal.payload_hash


def _build(cls: type, row: Mapping[str, Any]) -> Any:
    fields = cls.__dataclass_fields__
    try:
        return cls(**{name: row[name] for name in fields})
    except (KeyError, TypeError):
        return None


def _read_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _rpc_error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": _JSONRPC_VERSION, "id": mid, "error": {"code": code, "message": message}}


class _McpHandler(http.server.BaseHTTPRequestHandler):
    """Thin ``http.server`` adapter around :meth:`McpServer.handle_message`."""

    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, server: McpServer, **kwargs: Any) -> None:
        self._server = server
        super().__init__(*args, **kwargs)

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None and not _origin_ok(origin):
            self._respond(403, {"jsonrpc": _JSONRPC_VERSION, "id": None,
                                "error": {"code": -32000, "message": "forbidden origin"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond(400, _rpc_error(None, -32700, "parse error"))
            return
        self._respond(200, self._server.handle_message(message))

    def _respond(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log; the audit ledger is the log."""


def _origin_ok(origin: str) -> bool:
    """A browser Origin must be loopback; a missing Origin is a local client."""
    parsed = urllib.parse.urlsplit(str(origin))
    host = parsed.hostname or urllib.parse.urlsplit(f"//{origin}").hostname
    return bool(host) and _is_loopback(host)
