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


def test_allocation_percent_tolerance():
    rd = parse_research_decision(_sample(allocation_pct=55.0))
    c = build_signal_contract(rd, "2027-01-01", datetime(2026, 9, 3, 12, 0), None)
    assert c.target_pct == 0.55


def test_notional_from_equity_when_no_target():
    doc = _sample(allocation_pct=0.1, direction="add")
    rd = parse_research_decision(doc)
    c = build_signal_contract(rd, "2027-01-01", datetime(2026, 9, 3, 12, 0), book_equity=100000.0)
    assert c.target_notional_usd == 10000.0
    assert c.action == "BUY"
    assert c.implies_short is False
