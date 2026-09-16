"""The control-surface host: `signald api` and `signald mcp` (plan §7, §12.3-12.4).

``api_enabled``/``mcp_enabled`` are permission flags; this module is the host
that makes them mean something. Both surfaces are assembled from the same
honest pieces:

* **local state** - the canonical signal feed, the pending-order store under
  ``data_dir``, the kill-switch sentinel + HALT episode and the config hash.
  Broker-backed resources (``account``, ``positions``, ``risk``, ``sleeves``)
  read as ``None``: the live broker/market-data adapters are unwritten (P5 work)
  and a fabricated number is worse than a missing one.
* **halt** - the sentinel + episode latch, shared with ``signald halt``.
* **no order-path effects.** ``submit``/``cancel``/``ceiling`` and MCP's
  ``submit_order``/``set_sleeve_allocation`` stay unwired, so each server answers
  with its existing fail-closed refusal (503 ``api_disabled``; ``*_unavailable``)
  rather than a half-wired path sitting behind a flag.

The API's configured key (``TRADINGEXEC_API_KEY_ID`` +
``TRADINGEXEC_API_SIGNING_SECRET``) is the operator's ``admin`` key; role-scoped
keys belong in the OS keystore (plan §7.2) before any mutating route is wired.
Both launchers bind loopback only - each server refuses any other host *before*
creating a socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .api.server import ControlApi
from .api.signing import Authenticator, NonceStore
from .config import Config
from .kill_switch import ensure_episode, is_halted, read_episode
from .mcp.server import McpServer
from .stores import AuditChain

#: Single-use request nonce TTL (plan §7.2: 128-bit nonce, TTL 600 s).
NONCE_TTL_S = 600.0

#: The role this process gives its one configured key (plan §7.2 role set).
CONFIGURED_KEY_ROLE = "admin"


class ControlSurfaceError(ValueError):
    """A launcher that must not start: switch off, no key, or a bad bind."""


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
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


def local_state(cfg: Config) -> dict[str, Any]:
    """The read snapshot both surfaces serve (plan §7.1 read group).

    Keys are the endpoint/tool names: ``account``, ``positions``, ``risk``,
    ``sleeves``, ``signal`` (per-symbol map) and ``orders``. Anything that needs
    a broker or a reconcile pass is ``None`` - absent, never invented.
    """
    signals: dict[str, Any] = {}
    latest = cfg.data_dir / "latest.json"
    if latest.exists():
        try:
            loaded = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            signals = {str(k): v for k, v in loaded.items()}
    return {
        "signals": signals,
        "orders": _read_rows(cfg.data_dir / "orders_pending.jsonl"),
        "kill_switch": {
            "halted": is_halted(cfg.kill_switch_path),
            "episode": read_episode(cfg.halt_latch_path),
        },
        "config_hash": cfg.config_hash(),
        # No broker/market-data adapter yet (P5 deployment work).
        "account": None,
        "positions": None,
        "risk": None,
        "sleeves": None,
    }


def halt_now(cfg: Config, *, operator: str = "operator", action: str = "halt") -> dict[str, Any]:
    """Engage the kill switch: sentinel + episode latch + audit row.

    One implementation, shared by ``signald halt`` and both control surfaces, so
    an API/MCP halt leaves exactly the same trail as an operator halt.
    """
    sentinel = Path(cfg.kill_switch_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    stamp = cfg.now().isoformat(timespec="seconds")
    sentinel.write_text(f"halted by {operator} at {stamp}\n", encoding="utf-8")
    episode = ensure_episode(cfg.halt_latch_path, cfg.now())
    AuditChain(cfg.audit_file, cfg.now).append(
        "kill_switch", f"{action} by {operator}", episode=episode["episode"]
    )
    return {"halted": True, "episode": episode["episode"], "since": episode["since"]}


def build_control_api(cfg: Config) -> ControlApi:
    """The signed control API with the seams this repo can honestly serve.

    Raises :class:`ControlSurfaceError` when the switch is off or the signing key
    is incomplete - an unauthenticated control surface must never be built.
    """
    if not cfg.api_enabled:
        raise ControlSurfaceError("TRADINGEXEC_API_ENABLED is false")
    if not (cfg.api_key_id and cfg.api_signing_secret):
        raise ControlSurfaceError(
            "TRADINGEXEC_API_KEY_ID and TRADINGEXEC_API_SIGNING_SECRET are required "
            "(signed requests have no key to verify without them)"
        )
    key_id = str(cfg.api_key_id)
    authenticator = Authenticator(
        {key_id: str(cfg.api_signing_secret)},
        {key_id: CONFIGURED_KEY_ROLE},
        replay_window_s=cfg.api_replay_window_s,
        nonce_store=NonceStore(
            cfg.audit_file.parent / "api_nonces.jsonl", ttl_s=NONCE_TTL_S, now=cfg.now
        ),
        now=cfg.now,
    )
    return ControlApi(
        authenticator=authenticator,
        state=lambda: local_state(cfg),
        halt=lambda: halt_now(cfg, operator=f"api:{key_id}"),
        audit=AuditChain(cfg.audit_file, cfg.now),
        enabled=cfg.api_enabled,
        bind=cfg.api_bind,
    )


def build_mcp_server(cfg: Config) -> McpServer:
    """The MCP surface: the configured toolsets over local state + halt.

    ``submit_order``/``set_sleeve_allocation`` have no seam (no broker adapter),
    and ``simulate``/``propose`` have no gate seam, so each answers
    ``*_unavailable`` instead of inventing a verdict. Raises
    :class:`ControlSurfaceError` when the switch is off (and ``McpServer`` itself
    raises on an unknown toolset name, before a socket exists).
    """
    if not cfg.mcp_enabled:
        raise ControlSurfaceError("TRADINGEXEC_MCP_ENABLED is false")
    audit_dir = cfg.audit_file.parent
    return McpServer(
        toolsets=cfg.mcp_toolsets,
        state=lambda: local_state(cfg),
        halt=lambda: halt_now(cfg, operator="mcp"),
        proposals_path=audit_dir / "proposals.jsonl",
        approvals_path=audit_dir / "approvals.jsonl",
        audit=AuditChain(cfg.audit_file, cfg.now),
        now=cfg.now,
        approval_ttl_s=cfg.approval_ttl_s,
    )
