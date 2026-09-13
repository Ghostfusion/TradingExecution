"""The MCP tool table: plan §7.3 exactly, allow-list filtering, anti-rug-pull pin,
and the design-D6 refusal of every LLM-authored trading number."""

from __future__ import annotations

from dataclasses import replace

import pytest

from signald.mcp.tools import (
    APPROVAL_MODES,
    CLASSES,
    LLM_FORBIDDEN_NUMERIC_FIELDS,
    TOOL_TABLE,
    TOOLSETS,
    ToolSpec,
    assert_descriptions_unchanged,
    description_hash,
    refuses_llm_numbers,
    tools_for,
)

pytestmark = pytest.mark.timeout(120)

#: Plan §7.3 as data: name -> (class, approval, readOnly, destructive, idempotent, openWorld).
PLAN: dict[str, tuple[str, str, bool, bool, bool, bool]] = {
    "get_account_state": ("read", "no", True, False, True, False),
    "get_positions": ("read", "no", True, False, True, False),
    "get_open_orders": ("read", "no", True, False, True, False),
    "get_signal_latest": ("read", "no", True, False, True, False),
    "get_risk_state": ("read", "no", True, False, True, False),
    "get_sleeve_allocation": ("read", "no", True, False, True, False),
    "simulate_order": ("dry_run", "no", True, False, True, False),
    "propose_order": ("write_intent", "no", False, False, False, False),
    "submit_order": ("mutating", "yes", False, True, False, True),
    "cancel_order": ("mutating", "yes", False, True, True, True),
    "halt_trading": ("safety", "always", False, False, True, False),
    "set_sleeve_allocation": ("config", "yes", False, True, True, False),
}


def spec(name: str) -> ToolSpec:
    return next(s for s in TOOL_TABLE if s.name == name)


def test_the_table_is_the_plan_and_nothing_else():
    assert [s.name for s in TOOL_TABLE] == list(PLAN)
    for name, expected in PLAN.items():
        got = spec(name)
        assert (got.class_, got.approval, got.read_only, got.destructive, got.idempotent,
                got.open_world) == expected, name
        assert got.class_ in CLASSES and got.approval in APPROVAL_MODES
        assert got.toolset in TOOLSETS
        assert got.description.strip() and isinstance(got.input_schema, dict)


def test_only_the_mutating_and_config_tools_carry_an_approval():
    needing = {s.name for s in TOOL_TABLE if s.approval == "yes"}
    assert needing == {"submit_order", "cancel_order", "set_sleeve_allocation"}
    assert spec("halt_trading").approval == "always"


def test_read_only_toolsets_exclude_every_mutating_tool():
    allowed = tools_for("read")
    assert [s.name for s in allowed] == [
        "get_account_state", "get_positions", "get_open_orders",
        "get_signal_latest", "get_risk_state", "get_sleeve_allocation",
    ]
    assert not any(s.class_ in {"mutating", "config"} for s in allowed)
    assert "safety" not in {s.toolset for s in allowed}


def test_mutating_tools_exist_only_when_their_toolset_is_listed():
    default = {s.name for s in tools_for("read,simulate,propose")}
    assert default == {
        "get_account_state", "get_positions", "get_open_orders", "get_signal_latest",
        "get_risk_state", "get_sleeve_allocation", "simulate_order", "propose_order",
    }
    assert {s.name for s in tools_for("mutate")} == {"submit_order", "cancel_order"}
    assert {s.name for s in tools_for("config")} == {"set_sleeve_allocation"}
    assert {s.name for s in tools_for("safety")} == {"halt_trading"}
    assert {s.name for s in tools_for("read,simulate,propose,mutate,config,safety")} == set(PLAN)


@pytest.mark.parametrize("toolsets", ["admin", "read,admin", "shell", "MUTATE!"])
def test_an_unknown_toolset_is_refused(toolsets):
    with pytest.raises(ValueError):
        tools_for(toolsets)


def test_the_description_pin_is_deterministic_and_flags_a_change():
    pinned = {s.name: description_hash(s) for s in TOOL_TABLE}
    assert_descriptions_unchanged(pinned)  # unchanged table passes
    assert description_hash(spec("propose_order")) == description_hash(spec("propose_order"))

    tampered = dict(pinned)
    tampered["propose_order"] = "0" * 64
    with pytest.raises(ValueError, match="propose_order"):
        assert_descriptions_unchanged(tampered)


def test_the_pin_flags_a_removed_tool():
    with pytest.raises(ValueError, match="simulate_order"):
        assert_descriptions_unchanged({"simulate_order": "not-the-real-hash"})


def test_a_changed_description_changes_the_hash():
    original = spec("halt_trading")
    edited = replace(original, description=original.description + " (rug-pull)")
    assert description_hash(edited) != description_hash(original)


def test_a_changed_schema_changes_the_hash():
    original = spec("get_signal_latest")
    edited = replace(original, input_schema={"type": "object", "properties": {}})
    assert description_hash(edited) != description_hash(original)


def test_an_llm_intent_payload_is_accepted():
    assert refuses_llm_numbers(
        {"symbol": "AAPL", "side": "buy", "intent": "open", "rationale": "orm breakout"}
    ) is None


@pytest.mark.parametrize("key", ["qty", "quantity", "shares", "price", "limit_price", "stop",
                                 "stop_price", "take_profit", "notional", "target_notional"])
def test_every_llm_trading_number_is_refused_by_name(key):
    assert refuses_llm_numbers(
        {"symbol": "AAPL", "side": "buy", "intent": "open", key: 100.0}
    ) == key
    assert key in LLM_FORBIDDEN_NUMERIC_FIELDS


def test_an_unknown_numeric_field_is_refused():
    assert refuses_llm_numbers(
        {"symbol": "AAPL", "side": "buy", "intent": "open", "frobnicate": 12}
    ) == "frobnicate"


def test_a_nested_numeric_trading_field_is_refused():
    assert refuses_llm_numbers(
        {"symbol": "AAPL", "side": "buy", "intent": "open", "position": {"stop_loss": 429.0}}
    ) == "stop_loss"


def test_a_forbidden_field_is_refused_even_without_a_number():
    assert refuses_llm_numbers({"symbol": "AAPL", "side": "buy", "intent": "open",
                                "notional": None}) == "notional"


def test_a_non_numeric_unknown_field_is_tolerated():
    assert refuses_llm_numbers(
        {"symbol": "AAPL", "side": "buy", "intent": "open", "note": "from the desk"}
    ) is None
