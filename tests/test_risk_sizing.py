"""P1 sizer tests: the one place a quantity is produced (plan §4.3, design §7.2).

Every number asserted here is worked by hand in the test body or the comment
above it; nothing is copied out of the implementation.
"""

from __future__ import annotations

import pytest

from signald.risk.sizing import SizingCaps, kelly_cap, size
from signald.risk.state import RiskRequest

pytestmark = pytest.mark.timeout(120)


def caps(**over) -> SizingCaps:
    base: dict = {
        "sleeve_risk_remaining_pct": 0.02,
        "house_heat_remaining_pct": 0.03,
        "sleeve_notional_room_usd": 100_000.0,
        "participation_cap_pct": 0.05,
        "kelly_fraction": 0.25,
        "validated_edge": 0.20,
        "kelly_win_loss_ratio": 2.0,
        "kelly_shrink": 1.0,
    }
    base.update(over)
    return SizingCaps(**base)


def req(**over) -> RiskRequest:
    base: dict = {
        "sleeve": "intraday",
        "symbol": "MSFT",
        "setup": "ORB",
        "side": "buy",
        "stop_distance": 5.0,
        "price": 50.0,
        "equity": 100_000.0,
        "requested_risk_pct": 0.01,
        "trailing_volume": 20_000.0,
    }
    base.update(over)
    return RiskRequest(**base)


# --- the happy path, worked by hand ----------------------------------------
def test_hand_worked_happy_path():
    """min(1%, 2%, 3%) = 1%; Kelly = 0.25 * (2*0.4 - 0.6)/2 = 0.025 (does not bind);
    qty_risk = 100_000 * 0.01 / 5 = 200; qty_liq = 0.05 * 20_000 = 1_000;
    qty_notional = 100_000 / 50 = 2_000; floor(min) = 200."""
    d = size(req(), caps=caps())
    assert d.ok and d.reason == "ok"
    assert d.qty == 200
    assert d.risk_pct == pytest.approx(0.01)
    assert d.binding == "risk"
    assert d.qty_risk == pytest.approx(200.0)
    assert d.qty_liquidity == pytest.approx(1_000.0)
    assert d.qty_notional == pytest.approx(2_000.0)


# --- each cap binds in turn ------------------------------------------------
@pytest.mark.parametrize(
    ("cap_over", "req_over", "vol", "expected"),
    [
        ({}, {}, 1.0, "risk"),
        ({"sleeve_risk_remaining_pct": 0.004}, {}, 1.0, "sleeve"),
        ({"house_heat_remaining_pct": 0.005}, {}, 1.0, "heat"),
        ({}, {}, 0.5, "vol"),
        ({"validated_edge": 0.05}, {}, 1.0, "kelly"),
        ({}, {"trailing_volume": 1_000.0}, 1.0, "liquidity"),
        ({"sleeve_notional_room_usd": 5_000.0}, {}, 1.0, "notional"),
        ({"max_leverage_pct": 0.05}, {}, 1.0, "leverage"),
    ],
)
def test_each_cap_binds_in_turn(cap_over, req_over, vol, expected):
    d = size(req(**req_over), caps=caps(**cap_over), vol_scalar=vol)
    assert d.ok, d.reason
    assert d.binding == expected


# --- rejections are fail-closed --------------------------------------------
def test_missing_stop_rejects_and_is_never_widened():
    d = size(req(stop_distance=0.0), caps=caps())
    assert not d.ok and d.qty == 0 and d.reason == "no_stop_distance"
    assert d.risk_pct == 0.0 and d.binding is None
    assert d.qty_risk == 0.0 and d.qty_liquidity == 0.0 and d.qty_notional == 0.0


def test_negative_stop_rejects():
    assert size(req(stop_distance=-3.0), caps=caps()).reason == "no_stop_distance"


def test_nan_stop_rejects():
    assert size(req(stop_distance=float("nan")), caps=caps()).reason == "no_stop_distance"


def test_unvalidated_setup_rejects():
    """An unvalidated setup's Kelly input is 0, so its size is 0."""
    d = size(req(), caps=caps(validated_edge=None))
    assert not d.ok and d.qty == 0 and d.reason == "unvalidated_setup"
    assert d.binding is None


@pytest.mark.parametrize("volume", [None, 0.0])
def test_missing_or_zero_trailing_volume_rejects(volume):
    """No volume data -> the liquidity candidate is 0 -> fail closed."""
    d = size(req(trailing_volume=volume), caps=caps())
    assert not d.ok and d.qty == 0 and d.reason == "size_below_one_share"
    assert d.qty_liquidity == 0.0


@pytest.mark.parametrize("bad", [0.0, -0.5, float("nan")])
def test_non_positive_vol_scalar_rejects(bad):
    d = size(req(), caps=caps(), vol_scalar=bad)
    assert not d.ok and d.qty == 0 and d.reason == "size_below_one_share"
    assert d.binding is None


def test_below_one_share_rejects_never_rounds_up():
    # qty_risk = 100 * 0.01 / 5 = 0.2 shares -> reject, not 1.
    d = size(req(equity=100.0), caps=caps())
    assert not d.ok and d.qty == 0 and d.reason == "size_below_one_share"
    assert d.qty_risk == pytest.approx(0.2)
    assert d.risk_pct == 0.0
    assert d.binding == "risk"  # a cap, not an input, drove it below one share


def test_floor_never_rounds_up():
    # qty_risk = 199 * 0.01 / 1 = 1.99 shares -> floor 1, never 2.
    d = size(
        req(equity=199.0, stop_distance=1.0, price=10.0, trailing_volume=1_000_000.0),
        caps=caps(sleeve_notional_room_usd=1_000_000.0),
    )
    assert d.ok and d.qty == 1
    assert d.qty_risk == pytest.approx(1.99)


@pytest.mark.parametrize("price", [0.0, -50.0, float("nan")])
def test_bad_price_rejects(price):
    d = size(req(price=price), caps=caps())
    assert not d.ok and d.qty == 0 and d.reason == "no_price"


# --- fractional Kelly ------------------------------------------------------
def test_hand_worked_kelly_cap():
    # edge = p*R - q = 0.4*2 - 0.6 = 0.2  =>  p = 1.2/3 = 0.4
    # f* = (2*0.4 - 0.6)/2 = 0.1; half-Kelly = 0.5 * 0.1 = 0.05
    assert kelly_cap(edge=0.2, fraction=0.5, win_loss_ratio=2.0) == pytest.approx(0.05)
    # quarter-Kelly with a half estimation-error haircut = 0.25 * 0.1 * 0.5
    assert kelly_cap(
        edge=0.2, fraction=0.25, win_loss_ratio=2.0, shrink=0.5
    ) == pytest.approx(0.0125)
    # even money (the default ratio): p = 1.1/2 = 0.55, f* = 0.1
    assert kelly_cap(edge=0.1, fraction=1.0) == pytest.approx(0.1)


@pytest.mark.parametrize("edge", [0.0, -0.05, -0.2, -5.0])
def test_kelly_cap_is_zero_for_a_non_positive_edge(edge):
    assert kelly_cap(edge=edge, fraction=0.25, win_loss_ratio=2.0) == 0.0


@pytest.mark.parametrize(
    ("edge", "fraction", "ratio", "shrink"),
    [
        (0.2, 0.0, 2.0, 1.0),
        (0.2, -0.25, 2.0, 1.0),
        (0.2, 0.25, 0.0, 1.0),
        (0.2, 0.25, -1.0, 1.0),
        (0.2, 0.25, 2.0, 0.0),
        (float("nan"), 0.25, 2.0, 1.0),
    ],
)
def test_kelly_cap_fails_closed_on_unusable_inputs(edge, fraction, ratio, shrink):
    assert kelly_cap(edge=edge, fraction=fraction, win_loss_ratio=ratio, shrink=shrink) == 0.0


def test_small_edge_kelly_binds_and_a_negative_edge_sizes_to_zero():
    # edge = 0.05, R = 2  =>  p = 1.05/3 = 0.35, f* = (0.7 - 0.65)/2 = 0.025
    # quarter-Kelly = 0.00625 < the 1% request -> Kelly is binding
    d = size(req(), caps=caps(validated_edge=0.05))
    assert d.ok and d.binding == "kelly"
    assert d.risk_pct == pytest.approx(0.00625)
    # edge = -0.2 -> no bet -> risk 0 -> below one share
    d = size(req(), caps=caps(validated_edge=-0.2))
    assert not d.ok and d.qty == 0 and d.reason == "size_below_one_share"


# --- invariants ------------------------------------------------------------
INVARIANT_CASES = [
    ({}, {}, 1.0),
    ({}, {}, 0.5),
    ({}, {}, 1.5),
    ({"max_leverage_pct": 0.05}, {}, 1.0),
    ({}, {"trailing_volume": 1_000.0}, 1.0),
    ({"sleeve_notional_room_usd": 5_000.0}, {}, 1.0),
    ({"validated_edge": 0.05}, {}, 1.0),
    ({"sleeve_risk_remaining_pct": 0.004, "house_heat_remaining_pct": 0.005}, {}, 1.0),
    ({}, {"equity": 199.0, "stop_distance": 1.0, "price": 10.0}, 1.0),
]


@pytest.mark.parametrize(("cap_over", "req_over", "vol"), INVARIANT_CASES)
def test_quantity_never_exceeds_any_cap(cap_over, req_over, vol):
    c = caps(**cap_over)
    r = req(**req_over)
    d = size(r, caps=c, vol_scalar=vol)
    if not d.ok:
        assert d.qty == 0
        return
    price = r.notional_per_share
    # the floor is at or below every pre-floor candidate
    assert d.qty <= d.qty_risk + 1e-9
    assert d.qty <= d.qty_liquidity + 1e-9
    assert d.qty <= d.qty_notional + 1e-9
    # liquidity participation, sleeve notional room, leverage ceiling
    assert d.qty <= c.participation_cap_pct * (r.trailing_volume or 0.0) + 1e-9
    assert d.qty * price <= c.sleeve_notional_room_usd + 1e-9
    if c.max_leverage_pct is not None:
        assert d.qty * price <= r.equity * c.max_leverage_pct + 1e-9
    # the deployed risk is inside the same risk fraction the decision reports
    assert d.qty * r.stop_distance <= r.equity * d.risk_pct + 1e-9
    # the risk fraction stays inside the scaled budget it was cut from
    budget = min(
        r.requested_risk_pct, c.sleeve_risk_remaining_pct, c.house_heat_remaining_pct
    )
    assert budget > 0.0
    assert d.risk_pct <= budget * vol + 1e-9
    # and never past what the validation record's Kelly allows
    assert d.risk_pct <= kelly_cap(
        edge=c.validated_edge,
        fraction=c.kelly_fraction,
        win_loss_ratio=c.kelly_win_loss_ratio or 1.0,
        shrink=c.kelly_shrink,
    ) + 1e-9
