"""Schema + normalizer unit tests (pure, no network)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from signald.samples import build_sample
from signald.schema import (
    ContractError,
    build_signal_contract,
    parse_research_decision,
)

pytestmark = pytest.mark.timeout(120)


def _sample(**kw):
    return build_sample(**kw)


def test_parse_valid_artifact():
    rd = parse_research_decision(_sample())
    assert rd.ticker == "AVGO"
    assert rd.effective_date == date.today()
    assert rd.action() == "REDUCE"
    assert rd.decision_hash  # hash computed + verified
    assert rd.stop_loss == 320.0
    assert rd.data_quality == "fresh"


def test_parse_tolerates_sha256_prefix():
    doc = _sample()
    parse_research_decision(doc)  # build_sample already emits sha256: prefixed hash


def test_hash_mismatch_rejected():
    doc = _sample()
    doc["decision_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ContractError, match="decision_hash mismatch"):
        parse_research_decision(doc)


def test_missing_ticker_rejected():
    doc = _sample()
    del doc["ticker"]
    with pytest.raises(ContractError, match="ticker"):
        parse_research_decision(doc)


def test_bad_date_rejected():
    doc = _sample(effective_date=date.today())
    doc["effective_date"] = "not-a-date"
    with pytest.raises(ContractError, match="effective_date"):
        parse_research_decision(doc)


def test_bad_data_quality_rejected():
    doc = _sample()
    doc["data_quality"] = "bogus"
    with pytest.raises(ContractError, match="data_quality"):
        parse_research_decision(doc)


def test_action_resolution_from_rating():
    doc = _sample(direction=None, rating="Buy")
    rd = parse_research_decision(doc)
    assert rd.action() == "BUY"
    doc = _sample(direction=None, rating="Sell")
    assert parse_research_decision(doc).action() == "EXIT"


def test_no_direction_no_rating_rejected():
    doc = _sample()
    doc.pop("direction", None)
    doc.pop("rating", None)
    with pytest.raises(ContractError, match="cannot resolve"):
        parse_research_decision(doc)


def test_build_signal_contract_maps_fields():
    rd = parse_research_decision(_sample())
    c = build_signal_contract(rd, "2027-01-01", datetime(2026, 9, 3, 12, 0), book_equity=110000.0)
    assert c.symbol == "AVGO"
    assert c.action == "REDUCE"
    assert c.stop_price == 320.0
    assert c.expiry == "2027-01-01"
    assert c.target_pct == 0.0  # recommended_allocation_pct 0.0
    assert c.target_notional_usd == 0.0


def test_allocation_pct_is_a_percent_at_every_scale():
    """The unit is decided: ``recommended_allocation_pct`` is 0..100 PERCENT.

    The producer's own schema declares it ``ge=0, le=100``, ``reporting.py``
    multiplies the book fraction by 100, and the authoritative contract
    (``contracts/research_decision.v1.schema.json``, owner-designated 2026-10-03)
    states "in percent". The previous conversion fired only when ``pct > 1.0``,
    so every allocation in (0, 1] percent was read as a fraction - a 100x error.
    """
    for percent, expected in (
        (55.0, 0.55),
        (2.42, 0.0242),
        (100.0, 1.0),
        (1.0, 0.01),
        (0.5, 0.005),
        (0.1, 0.001),
        (0.0, 0.0),
    ):
        rd = parse_research_decision(_sample(allocation_pct=percent))
        c = build_signal_contract(rd, "2027-01-01", datetime(2026, 9, 3, 12, 0), None)
        assert c.target_pct == pytest.approx(expected), (percent, c.target_pct)


def test_a_sub_one_percent_allocation_is_not_read_as_a_fraction():
    """Failing-first for the (0, 1] percent band: 0.5% is 0.005, never 0.5.

    Before the fix this returned 0.5 (50%) and derived a $50,000 notional on a
    $100,000 book from a 0.5% instruction.
    """
    rd = parse_research_decision(_sample(allocation_pct=0.5))
    c = build_signal_contract(rd, "2027-01-01", datetime(2026, 9, 3, 12, 0), book_equity=100000.0)
    assert c.target_pct == pytest.approx(0.005)
    assert c.target_notional_usd == pytest.approx(500.0)


def test_notional_from_equity_when_no_target():
    doc = _sample(allocation_pct=2.0, direction="add")  # 2.0 PERCENT, not a fraction
    rd = parse_research_decision(doc)
    c = build_signal_contract(rd, "2027-01-01", datetime(2026, 9, 3, 12, 0), book_equity=100000.0)
    assert c.target_notional_usd == 2000.0
    assert c.action == "BUY"
    assert c.implies_short is False
