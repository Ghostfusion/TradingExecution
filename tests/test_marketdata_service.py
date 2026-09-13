"""Market-data service: provenance, freshness, quarantine (plan §4.1 C4, design §9).

Every read goes through an injected fake transport - no network, no credentials,
and the clock is the `now` fixture. Assertions are on the observable result: the
Quote/Bar a caller receives, or the fail-closed MarketDataUnavailable that
replaces it. A None transport return, a missing field, an unparseable number, a
foreign feed, a stale stamp and a future-dated bar are all failures here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from signald.marketdata.service import (
    Bar,
    MarketDataService,
    MarketDataUnavailable,
    Quote,
)

pytestmark = pytest.mark.timeout(120)


class _Transport:
    """Records (method, payload); returns the configured body (None = unavailable)."""

    def __init__(self, *, quote=None, bars=None):
        self.quote_body = quote
        self.bars_body = bars
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "quote":
            return self.quote_body
        if method == "bars":
            return self.bars_body
        raise AssertionError(f"unexpected method {method}")


def _quote_body(now, *, feed="sip", bid=99.98, ask=100.02, last=100.0, ts=None):
    stamp = now if ts is None else ts
    return {
        "symbol": "AAPL",
        "feed": feed,
        "as_of": stamp.isoformat() if hasattr(stamp, "isoformat") else stamp,
        "bid": bid,
        "ask": ask,
        "last": last,
    }


def _bar_row(ts, o=10.0, h=11.0, low=9.0, c=10.5, v=100.0):
    return {
        "ts": ts.isoformat(),
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": v,
    }


def _bars_body(rows, *, feed="sip"):
    return {"symbol": "AAPL", "feed": feed, "bars": rows}


# --- quotes ---------------------------------------------------------------
def test_quote_happy_path_reports_mid_and_spread(cfg, now):
    transport = _Transport(quote=_quote_body(now))
    quote = MarketDataService(transport, cfg.now, cfg).quote("AAPL")

    assert isinstance(quote, Quote)
    assert quote.mid == pytest.approx(100.0)
    # (ask - bid) / mid * 10_000 = (0.04 / 100.0) * 10_000
    assert quote.spread_bps == pytest.approx(4.0)
    assert quote.age_s == pytest.approx(0.0)
    assert quote.feed == "sip"
    assert transport.calls == [("quote", {"symbol": "AAPL"})]


def test_quote_without_depth_keeps_mid_none(cfg, now):
    body = _quote_body(now, bid=None, ask=None, last=100.0)
    quote = MarketDataService(_Transport(quote=body), cfg.now, cfg).quote("AAPL")
    assert quote.mid is None
    assert quote.spread_bps is None
    assert quote.last == pytest.approx(100.0)


def test_transport_none_is_unavailable(cfg, now):
    transport = _Transport(quote=None)
    with pytest.raises(MarketDataUnavailable):
        MarketDataService(transport, cfg.now, cfg).quote("AAPL")


def test_stale_quote_raises_unless_allowed(cfg, now):
    transport = _Transport(quote=_quote_body(now, ts=now - timedelta(seconds=5)))
    service = MarketDataService(transport, cfg.now, cfg)

    with pytest.raises(MarketDataUnavailable):
        service.quote("AAPL")
    quote = service.quote("AAPL", allow_stale=True)
    assert quote.age_s == pytest.approx(5.0)


def test_feed_mismatch_raises_unless_allowed(cfg, now):
    transport = _Transport(quote=_quote_body(now, feed="iex"))
    service = MarketDataService(transport, cfg.now, cfg)

    with pytest.raises(MarketDataUnavailable):
        service.quote("AAPL")
    assert service.quote("AAPL", allow_feed_mismatch=True).feed == "iex"


def test_quote_without_any_price_raises(cfg, now):
    transport = _Transport(quote={"feed": "sip", "as_of": now.isoformat()})
    with pytest.raises(MarketDataUnavailable):
        MarketDataService(transport, cfg.now, cfg).quote("AAPL")


@pytest.mark.parametrize("key,bad", [("last", "n/a"), ("bid", ""), ("ask", "12,5")])
def test_unparseable_quote_number_raises(cfg, now, key, bad):
    body = _quote_body(now)
    body[key] = bad
    transport = _Transport(quote=body)
    with pytest.raises(MarketDataUnavailable):
        MarketDataService(transport, cfg.now, cfg).quote("AAPL")


def test_unparseable_quote_timestamp_raises(cfg, now):
    transport = _Transport(quote=_quote_body(now, ts="yesterday-ish"))
    with pytest.raises(MarketDataUnavailable):
        MarketDataService(transport, cfg.now, cfg).quote("AAPL")


# --- bars -----------------------------------------------------------------
def test_bars_are_ascending_and_request_is_point_in_time(cfg, now):
    rows = [_bar_row(now - timedelta(minutes=m)) for m in (2, 1, 0)]
    transport = _Transport(bars=_bars_body(rows))
    bars = MarketDataService(transport, cfg.now, cfg).bars("AAPL", count=3)

    assert all(isinstance(bar, Bar) for bar in bars)
    assert [bar.ts for bar in bars] == sorted(bar.ts for bar in bars)
    method, payload = transport.calls[0]
    assert method == "bars"
    assert payload == {
        "symbol": "AAPL",
        "count": 3,
        "timeframe": "1Min",
        "as_of": now.isoformat(),
    }


def test_daily_uses_the_day_timeframe_and_skips_intraday_staleness(cfg, now):
    rows = [_bar_row(now - timedelta(days=1))]
    transport = _Transport(bars=_bars_body(rows))
    bars = MarketDataService(transport, cfg.now, cfg).daily("AAPL", count=1)

    assert len(bars) == 1
    assert transport.calls[0][1]["timeframe"] == "1Day"


def test_future_bar_is_dropped_no_lookahead(cfg, now):
    rows = [
        _bar_row(now - timedelta(seconds=30)),
        _bar_row(now + timedelta(seconds=60)),
    ]
    transport = _Transport(bars=_bars_body(rows))
    bars = MarketDataService(transport, cfg.now, cfg).bars("AAPL", count=2, as_of=now)

    assert [bar.ts for bar in bars] == [now - timedelta(seconds=30)]


def test_a_window_of_only_future_bars_is_an_error(cfg, now):
    rows = [
        _bar_row(now + timedelta(seconds=60)),
        _bar_row(now + timedelta(seconds=120)),
    ]
    transport = _Transport(bars=_bars_body(rows))
    service = MarketDataService(transport, cfg.now, cfg)

    with pytest.raises(MarketDataUnavailable):
        service.bars("AAPL", count=2, as_of=now)


def test_stale_bars_raise_unless_allowed(cfg, now):
    rows = [_bar_row(now - timedelta(seconds=120))]
    transport = _Transport(bars=_bars_body(rows))
    service = MarketDataService(transport, cfg.now, cfg)

    with pytest.raises(MarketDataUnavailable):
        service.bars("AAPL", count=1)
    assert len(service.bars("AAPL", count=1, allow_stale=True)) == 1


def test_bars_feed_mismatch_raises_unless_allowed(cfg, now):
    rows = [_bar_row(now)]
    transport = _Transport(bars=_bars_body(rows, feed="iex"))
    service = MarketDataService(transport, cfg.now, cfg)

    with pytest.raises(MarketDataUnavailable):
        service.bars("AAPL", count=1)
    assert len(service.bars("AAPL", count=1, allow_feed_mismatch=True)) == 1


def test_missing_bars_field_raises(cfg, now):
    transport = _Transport(bars={"feed": "sip"})
    with pytest.raises(MarketDataUnavailable):
        MarketDataService(transport, cfg.now, cfg).bars("AAPL", count=1)


def test_unparseable_bar_number_raises(cfg, now):
    row = _bar_row(now)
    row["volume"] = "many"
    transport = _Transport(bars=_bars_body([row]))
    with pytest.raises(MarketDataUnavailable):
        MarketDataService(transport, cfg.now, cfg).bars("AAPL", count=1)


# --- quarantine -----------------------------------------------------------
def test_quarantine_blocks_the_next_read_without_touching_the_transport(cfg, now):
    transport = _Transport(quote=_quote_body(now), bars=_bars_body([_bar_row(now)]))
    service = MarketDataService(transport, cfg.now, cfg)

    assert service.is_quarantined("AAPL") is False
    service.quarantine("AAPL", "spread_blowout")
    assert service.is_quarantined("AAPL") is True

    with pytest.raises(MarketDataUnavailable):
        service.quote("AAPL")
    with pytest.raises(MarketDataUnavailable):
        service.bars("AAPL", count=1)
    assert transport.calls == []
