"""The OrderGuard: one test per check that refuses, plus the happy path.

Every check in `guard.CHECKS` that can refuse an order has a test here that
makes it refuse, so the guard's table and this file move together. Three
structural properties are pinned as separate tests: a market order is denied
*by construction*, the binding check is the **first** failure in table order
while all failures are collected, and a check that **raises** denies instead
of passing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from signald.order import guard as G
from signald.order.guard import OrderIntent, client_order_id
from signald.risk.state import SWING, BookState, MarketState, Position

pytestmark = pytest.mark.timeout(120)

CREATED_AT = "2026-09-12T13:45:02Z"


def intent(**kw) -> OrderIntent:
    base = {
        "intent_id": "int-1",
        "sleeve": SWING,
        "symbol": "MSFT",
        "side": "buy",
        "qty": 10,
        "order_type": "limit",
        "limit_price": 100.0,
        "stop_price": 95.0,
        "tif": "day",
        "client_order_id": client_order_id(SWING, "abc", "entry"),
        "created_at": CREATED_AT,
    }
    base.update(kw)
    return OrderIntent(**base)


def book(**kw) -> BookState:
    base = {"equity": 100_000.0, "cash": 40_000.0, "buying_power": 50_000.0}
    base.update(kw)
    return BookState(**base)


def market(**kw) -> MarketState:
    base = {"symbol": "MSFT", "last": 100.0}
    base.update(kw)
    return MarketState(**base)


def run(mandate, cfg, the_intent=None, **kw) -> G.GuardResult:
    the_intent = the_intent if the_intent is not None else intent()
    params = {
        "book": book(),
        "market": market(),
        "mandate": mandate,
        "config": cfg,
        "gate_verdict": "ALLOW",
    }
    params.update(kw)
    return G.guard(the_intent, **params)


# --- happy path ------------------------------------------------------------
def test_a_clean_intent_is_allowed(mandate, cfg):
    result = run(mandate, cfg)
    assert result.ok is True
    assert result.reasons == ()
    assert result.binding is None


def test_notional_is_price_times_qty_with_a_stop_fallback():
    assert intent(qty=7, limit_price=12.5).notional_usd == pytest.approx(87.5)
    assert intent(qty=2, limit_price=None, stop_price=90.0).notional_usd == pytest.approx(180.0)
    assert intent(qty=2, limit_price=None, stop_price=None).notional_usd == 0.0


# --- 1. client_order_id ----------------------------------------------------
def test_empty_client_order_id_denies(mandate, cfg):
    result = run(mandate, cfg, intent(client_order_id=""))
    assert result.ok is False and result.binding == "client_order_id"


def test_oversized_client_order_id_denies(mandate, cfg):
    result = run(mandate, cfg, intent(client_order_id="x" * 49))
    assert result.ok is False and result.binding == "client_order_id"


def test_client_order_id_with_illegal_characters_denies(mandate, cfg):
    result = run(mandate, cfg, intent(client_order_id="ord 1!"))
    assert result.ok is False and result.binding == "client_order_id"


def test_client_order_id_is_deterministic_bounded_and_safe():
    first = client_order_id("intraday", "k" * 40, "entry")
    assert first == client_order_id("intraday", "k" * 40, "entry")
    assert first.startswith("int-")
    assert len(first) <= 48
    assert G._ID_OK.match(first)  # charset [A-Za-z0-9-]
    long_leg = client_order_id("swing", "key", "e" * 60)
    assert len(long_leg) == 48
    assert long_leg != client_order_id("swing", "key", "exit")
    assert client_order_id("swing", "key", "entry") != client_order_id("intraday", "key", "entry")
    # illegal characters are stripped, not encoded
    cleaned = client_order_id("int rada y", "a b/c", "en try")
    assert cleaned == "int-abc-entry"


# --- 2. qty ----------------------------------------------------------------
@pytest.mark.parametrize("qty", [0, -3, 1.5, True])
def test_non_positive_or_fractional_qty_denies(mandate, cfg, qty):
    result = run(mandate, cfg, intent(qty=qty))
    assert result.ok is False and result.binding == "qty"


def test_qty_of_one_is_allowed(mandate, cfg):
    assert run(mandate, cfg, intent(qty=1)).ok is True


# --- 3. order_type ---------------------------------------------------------
def test_a_market_order_is_denied_by_construction(mandate, cfg):
    result = run(mandate, cfg, intent(order_type="market", limit_price=None))
    assert result.ok is False and result.binding == "order_type"


def test_an_unknown_order_type_denies(mandate, cfg):
    result = run(mandate, cfg, intent(order_type="peg"))
    assert result.ok is False and result.binding == "order_type"


# --- 4. precision ----------------------------------------------------------
@pytest.mark.parametrize("price", [100.001, 0.07155])
def test_a_sub_penny_limit_price_denies(mandate, cfg, price):
    result = run(mandate, cfg, intent(limit_price=price))
    assert result.ok is False and result.binding == "precision"


def test_a_sub_penny_stop_price_denies(mandate, cfg):
    result = run(mandate, cfg, intent(stop_price=95.001))
    assert result.ok is False and result.binding == "precision"


def test_a_whole_cent_price_passes_precision(mandate, cfg):
    assert run(mandate, cfg, intent(limit_price=100.01, stop_price=95.01)).ok is True


# --- 5. mandate ------------------------------------------------------------
def test_a_symbol_outside_the_mandate_denies(mandate, cfg):
    result = run(mandate, cfg, intent(symbol="TSLA"))
    assert result.ok is False and result.binding == "mandate"


def test_an_expired_mandate_denies(mandate, cfg):
    expired = replace(mandate, expires=date(2020, 1, 1))
    result = run(expired, cfg)
    assert result.ok is False and result.binding == "mandate"


def test_a_sell_that_would_open_a_short_denies(mandate, cfg):
    # the book holds no MSFT long, so a sell opens a short and the mandate
    # forbids shorts
    result = run(mandate, cfg, intent(side="sell", order_type="marketable_limit"))
    assert result.ok is False and result.binding == "mandate"


def test_a_sell_that_reduces_a_long_is_allowed(mandate, cfg):
    held = Position(symbol="MSFT", qty=10, avg_entry=90.0, last=100.0)
    result = run(mandate, cfg, intent(side="sell"), book=book(positions=(held,)))
    assert result.ok is True


def test_shorts_are_allowed_when_the_mandate_permits_them(mandate, cfg):
    shorting = replace(mandate, shorts=True)
    result = run(shorting, cfg, intent(side="sell", order_type="marketable_limit"))
    assert result.ok is True


def test_an_unknown_side_denies(mandate, cfg):
    result = run(mandate, cfg, intent(side="short"))
    assert result.ok is False and result.binding == "mandate"


# --- 6. gate_verdict -------------------------------------------------------
def test_a_missing_gate_verdict_denies(mandate, cfg):
    result = run(mandate, cfg, gate_verdict=None)
    assert result.ok is False and result.binding == "gate_verdict"


def test_a_block_verdict_denies(mandate, cfg):
    result = run(mandate, cfg, gate_verdict="BLOCK")
    assert result.ok is False and result.binding == "gate_verdict"


def test_a_reduce_verdict_is_permitted(mandate, cfg):
    assert run(mandate, cfg, gate_verdict="REDUCE").ok is True


# --- 7. approval -----------------------------------------------------------
def test_a_required_approval_denies_without_an_id(mandate, cfg):
    result = run(mandate, cfg, requires_approval=True)
    assert result.ok is False and result.binding == "approval"


def test_a_required_approval_passes_with_an_id(mandate, cfg):
    result = run(mandate, cfg, requires_approval=True, approval_id="apr-1")
    assert result.ok is True


def test_approval_is_not_required_by_default(mandate, cfg):
    assert run(mandate, cfg, approval_id=None).ok is True


# --- 8. buying_power -------------------------------------------------------
def test_notional_over_buying_power_denies(mandate, cfg):
    result = run(mandate, cfg, book=book(buying_power=500.0))
    assert result.ok is False and result.binding == "buying_power"


def test_the_leverage_cap_denies_a_buy(mandate, cfg):
    # 1,000 x 149.6 deployed + 1,000 new > 1.5 x 100,000
    held = Position(symbol="AAA", qty=1_000, avg_entry=149.6, last=149.6)
    result = run(mandate, cfg, book=book(positions=(held,)))
    assert result.ok is False and result.binding == "buying_power"


def test_a_buy_inside_the_leverage_cap_passes(mandate, cfg):
    held = Position(symbol="AAA", qty=1_000, avg_entry=100.0, last=100.0)
    assert run(mandate, cfg, book=book(positions=(held,))).ok is True


# --- 9. bracket_legality ---------------------------------------------------
def test_an_unsupported_tif_denies(mandate, cfg):
    result = run(mandate, cfg, intent(tif="ioc"))
    assert result.ok is False and result.binding == "bracket_legality"


def test_extended_hours_require_a_day_tif(mandate, cfg):
    result = run(mandate, cfg, intent(tif="gtc", extended_hours=True))
    assert result.ok is False and result.binding == "bracket_legality"


def test_extended_hours_reject_a_stop_leg(mandate, cfg):
    result = run(
        mandate,
        cfg,
        intent(order_type="stop", stop_price=95.0, extended_hours=True),
    )
    assert result.ok is False and result.binding == "bracket_legality"


def test_extended_hours_allow_a_limit(mandate, cfg):
    result = run(mandate, cfg, intent(extended_hours=True))
    assert result.ok is True


# --- 10. stop_present ------------------------------------------------------
def test_a_stop_less_entry_denies(mandate, cfg):
    result = run(mandate, cfg, intent(stop_price=None))
    assert result.ok is False and result.binding == "stop_present"


def test_a_stop_limit_is_its_own_stop(mandate, cfg):
    result = run(mandate, cfg, intent(order_type="stop_limit", stop_price=None))
    assert result.ok is True


# --- 11. duplicate ---------------------------------------------------------
def test_an_open_client_order_id_denies(mandate, cfg):
    cid = client_order_id(SWING, "abc", "entry")
    result = run(mandate, cfg, open_orders=(({"client_order_id": cid, "status": "accepted"}),))
    assert result.ok is False and result.binding == "duplicate"


def test_a_different_open_id_does_not_deny(mandate, cfg):
    result = run(mandate, cfg, open_orders=(({"client_order_id": "other-id"}),))
    assert result.ok is True


# --- 12. wash --------------------------------------------------------------
def test_an_opposite_sleeve_side_denies(mandate, cfg):
    result = run(mandate, cfg, other_sleeve_side="sell")
    assert result.ok is False and result.binding == "wash"


def test_the_same_sleeve_side_does_not_deny(mandate, cfg):
    assert run(mandate, cfg, other_sleeve_side="buy").ok is True


def test_a_sell_is_not_a_wash_against_a_sell(mandate, cfg):
    held = Position(symbol="MSFT", qty=10, avg_entry=90.0, last=100.0)
    result = run(
        mandate,
        cfg,
        intent(side="sell"),
        book=book(positions=(held,)),
        other_sleeve_side="sell",
    )
    assert result.ok is True


# --- structure -------------------------------------------------------------
def test_the_binding_check_is_the_first_failure_and_all_are_reported(mandate, cfg):
    # qty (2) precedes order_type (3); both fail
    result = run(mandate, cfg, intent(qty=0, order_type="market"))
    assert result.ok is False and result.binding == "qty"
    assert len(result.reasons) == 2
    assert any(r.startswith("order_type: ") for r in result.reasons)


def test_a_check_that_raises_denies(monkeypatch, mandate, cfg):
    def boom(_intent, _ctx):
        raise RuntimeError("check exploded")

    monkeypatch.setitem(G.CHECKS, "wash", boom)
    result = run(mandate, cfg)
    assert result.ok is False and result.binding == "wash"
    assert any("check_error" in r for r in result.reasons)


def test_a_poisoned_book_denies(mandate, cfg):
    result = run(mandate, cfg, book=None)
    assert result.ok is False and result.binding == "buying_power"
    assert any("check_error" in r for r in result.reasons)
