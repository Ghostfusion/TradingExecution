"""MCP tool table for the execution control surface (plan §7.3-7.4, design §12.3).

Invariant: a tool's MCP annotations are **untrusted hints**; the control is the
server-side allow-list (toolset membership, enforced in
:mod:`signald.mcp.server`). Tool descriptions are hash-pinned so a rug-pull is
detected on connect, an unknown toolset is refused (fail closed), and no number
an LLM authored may size, price or gate a trade (design D6).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from signald.schema import sha256_of

#: Tool classes (closed vocabulary). ``class_`` is one of these.
CLASSES: tuple[str, ...] = ("read", "dry_run", "write_intent", "mutating", "config", "safety")

#: Toolset names a caller may list on the wire (closed; anything else is refused).
TOOLSETS: tuple[str, ...] = ("read", "simulate", "propose", "mutate", "config", "safety")

#: Approval modes (closed vocabulary).
APPROVAL_MODES: tuple[str, ...] = ("no", "yes", "always")

#: The only keys an LLM-authored payload may carry numeric-free authority over.
#: ``qty``/``price``/``stop``/``notional`` are covered by the forbidden set below;
#: every other non-allowed numeric key is refused as well.
LLM_ALLOWED_AUTHORING_FIELDS: frozenset[str] = frozenset(
    {"symbol", "side", "intent", "rationale"}
)

#: Numeric trading fields an LLM payload may never carry (design D6, plan §12.3).
LLM_FORBIDDEN_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        "qty",
        "quantity",
        "shares",
        "size",
        "size_pct",
        "price",
        "entry_px",
        "entry_price",
        "limit_price",
        "avg_price",
        "fill_price",
        "stop",
        "stop_price",
        "stop_loss",
        "stop_distance",
        "target",
        "target_price",
        "take_profit",
        "notional",
        "target_notional",
        "notional_usd",
        "order_notional",
        "risk_pct",
        "requested_risk_pct",
        "risk_per_trade_pct",
        "allocation",
        "allocation_pct",
        "target_weight",
        "weight",
        "capital_pct",
        "leverage",
        "exposure",
    }
)


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool: its policy class, its hints and the toolset that enables it."""

    name: str
    class_: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool
    approval: str
    toolset: str


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = list(required)
    return out


_STRING = {"type": "string"}
_NUMBER = {"type": "number"}


def _read(
    name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()
) -> ToolSpec:
    return ToolSpec(
        name=name,
        class_="read",
        description=description,
        input_schema=_schema(properties, required),
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        approval="no",
        toolset="read",
    )


#: Plan §7.3, exactly: six read tools, simulate, propose, submit, cancel, halt, config.
TOOL_TABLE: tuple[ToolSpec, ...] = (
    _read("get_account_state", "Read-only: cash, equity, buying power and IML/IMD state.", {}),
    _read("get_positions", "Read-only: open positions, reconstructed from fills.", {}),
    _read("get_open_orders", "Read-only: working orders and their protective legs.", {}),
    _read(
        "get_signal_latest",
        "Read-only: the latest signal envelope for one symbol.",
        {"symbol": _STRING},
        ("symbol",),
    ),
    _read("get_risk_state", "Read-only: ES/CVaR usage, heat, drawdown rung and vol scalar.", {}),
    _read(
        "get_sleeve_allocation",
        "Read-only: per-sleeve ceiling, deployed percentage and scorecard summary.",
        {},
    ),
    ToolSpec(
        name="simulate_order",
        class_="dry_run",
        description=(
            "Dry-run: full house-gate evaluation with no side effects; the only way to "
            "test an idea. An LLM payload carries symbol/side/intent/rationale and no "
            "numeric trading field."
        ),
        input_schema=_schema(
            {"symbol": _STRING, "side": _STRING, "intent": _STRING, "rationale": _STRING},
            ("symbol", "side", "intent"),
        ),
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        approval="no",
        toolset="simulate",
    ),
    ToolSpec(
        name="propose_order",
        class_="write_intent",
        description=(
            "Write-intent: creates an immutable hash-pinned pending proposal; it cannot "
            "fill. An LLM payload carries symbol/side/intent/rationale and no numeric "
            "trading field; a BLOCK verdict creates no proposal."
        ),
        input_schema=_schema(
            {"symbol": _STRING, "side": _STRING, "intent": _STRING, "rationale": _STRING},
            ("symbol", "side", "intent"),
        ),
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        approval="no",
        toolset="propose",
    ),
    ToolSpec(
        name="submit_order",
        class_="mutating",
        description=(
            "Mutating: submits an approved proposal. The approval must be single-use, "
            "unexpired and bound to the proposal payload hash; a modified proposal is a "
            "different proposal."
        ),
        input_schema=_schema(
            {"proposal_id": _STRING, "approval_id": _STRING}, ("proposal_id", "approval_id")
        ),
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
        approval="yes",
        toolset="mutate",
    ),
    ToolSpec(
        name="cancel_order",
        class_="mutating",
        description="Mutating: cancels a working order; requires an approval token.",
        input_schema=_schema(
            {"order_id": _STRING, "approval_id": _STRING}, ("order_id", "approval_id")
        ),
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=True,
        approval="yes",
        toolset="mutate",
    ),
    ToolSpec(
        name="halt_trading",
        class_="safety",
        description=(
            "Safety: the kill switch - cancels working orders, flattens and latches HALT. "
            "Never gated and allowed even when its toolset was not listed."
        ),
        input_schema=_schema({}),
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        approval="always",
        toolset="safety",
    ),
    ToolSpec(
        name="set_sleeve_allocation",
        class_="config",
        description=(
            "Config: sets a sleeve capital ceiling. Operator-only; requires an "
            "operator approval token."
        ),
        input_schema=_schema(
            {"sleeve": _STRING, "capital_pct": _NUMBER, "approval_id": _STRING},
            ("sleeve", "capital_pct", "approval_id"),
        ),
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=False,
        approval="yes",
        toolset="config",
    ),
)


def _parse_toolsets(toolsets: str) -> frozenset[str]:
    """Parse a comma-separated toolset list; an unknown name is refused."""
    names = [part.strip().lower() for part in str(toolsets).split(",") if part.strip()]
    for name in names:
        if name not in TOOLSETS:
            raise ValueError(f"unknown MCP toolset: {name!r}")
    return frozenset(names)


def tools_for(toolsets: str) -> tuple[ToolSpec, ...]:
    """The tools a caller listing ``toolsets`` may see and call, in table order.

    A mutating/config tool exists only when its own toolset was listed; the
    safety toolset is not implied by any other. An unknown toolset name raises
    ``ValueError`` (fail closed).
    """
    wanted = _parse_toolsets(toolsets)
    return tuple(spec for spec in TOOL_TABLE if spec.toolset in wanted)


def description_hash(spec: ToolSpec) -> str:
    """The anti-rug-pull pin for one tool: hash of name + description + schema."""
    return sha256_of(spec.name + spec.description + json.dumps(spec.input_schema, sort_keys=True))


def assert_descriptions_unchanged(pinned: Mapping[str, str]) -> None:
    """Refuse to serve when a pinned tool description changed (design §12.2).

    Raises ``ValueError`` naming the first tool whose description (or removal)
    differs from the pin supplied by the operator.
    """
    current = {spec.name: description_hash(spec) for spec in TOOL_TABLE}
    for name in sorted(pinned):
        if current.get(name) != pinned[name]:
            raise ValueError(f"tool description changed: {name}")


def _numeric_like(value: Any) -> bool:
    """True for a real number or a numeric string; ``bool`` is not a number."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.strip())
        except ValueError:
            return False
        return True
    return False


def _numeric_key_in(obj: Any) -> str | None:
    """The first offending key: a forbidden trading field, or any smuggled number."""
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            name = str(key)
            lowered = name.strip().lower()
            if lowered not in LLM_ALLOWED_AUTHORING_FIELDS and (
                lowered in LLM_FORBIDDEN_NUMERIC_FIELDS or _numeric_like(value)
            ):
                return name
            nested = _numeric_key_in(value)
            if nested is not None:
                return nested
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            nested = _numeric_key_in(item)
            if nested is not None:
                return nested
    return None


def refuses_llm_numbers(payload: Mapping[str, Any]) -> str | None:
    """Return the offending key when an LLM payload carries trading authority.

    An LLM may author ``symbol``/``side``/``intent``/``rationale`` only: a
    present ``qty``/``price``/``stop``/``notional`` (or any other numeric
    trading field) is refused by name, so no LLM number can size, price or gate
    a trade (design D6).
    """
    return _numeric_key_in(payload)
