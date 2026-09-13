"""Market-data service: bars and quotes with provenance, freshness and quarantine.

Invariant (design §9, plan §4.1 C4): a decision may only ever cite data it
could have had. Every read is stamped ``as_of`` + ``feed`` and proven fresh,
or it raises :class:`MarketDataUnavailable`. There is no fabricated-price path:
a ``None`` from the transport, a missing or unparseable field, a foreign feed,
a stale stamp and a future-dated bar all fail closed. Bars are point-in-time -
a bar stamped after the decision cut-off is dropped, and a window entirely in
the future is an error, never an empty success.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import Config

#: The data seam: ``transport(method, payload) -> venue JSON body | None``.
#: ``None`` means unavailable and is always a failure, never an empty success.
MarketDataTransport = Callable[[str, dict[str, Any]], dict[str, Any] | None]

#: Response keys read here. Timestamps carry provenance (an ``as_of`` stamp;
#: ``ts`` is accepted because the Phase-A reference seam already emits it) and
#: ``feed`` names the venue the row came from.
_TS_KEY = "as_of"
_TS_ALT_KEY = "ts"
_FEED_KEY = "feed"
_BARS_KEY = "bars"


class MarketDataUnavailable(Exception):
    """A read could not be served with fresh, provenance-stamped data (fail closed)."""


@dataclass(frozen=True)
class Quote:
    """A top-of-book snapshot. Any absent field stays ``None`` - never a guess."""

    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    spread_bps: float | None
    as_of: datetime | None
    feed: str
    age_s: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``typical`` is the VWAP price weight."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


def _as_utc(value: datetime) -> datetime:
    """Normalise for age arithmetic; a naive stamp is taken as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        raise MarketDataUnavailable("missing_timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarketDataUnavailable(f"unparseable_timestamp:{value!r}") from exc


def _ts_of(row: dict[str, Any]) -> datetime:
    for key in (_TS_KEY, _TS_ALT_KEY):
        value = row.get(key)
        if value is not None:
            return _parse_ts(value)
    raise MarketDataUnavailable("missing_timestamp")


def _num(row: dict[str, Any], key: str, *, required: bool) -> float | None:
    value = row.get(key)
    if value is None:
        if required:
            raise MarketDataUnavailable(f"missing_field:{key}")
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataUnavailable(f"unparseable_number:{key}={value!r}") from exc


class MarketDataService:
    """Point-in-time reads with freshness, feed and quarantine enforcement."""

    def __init__(
        self,
        transport: MarketDataTransport,
        now: Callable[[], datetime],
        config: Config,
    ) -> None:
        self._transport = transport
        self._now = now
        self._config = config
        self._quarantined: dict[str, str] = {}

    # -- quarantine --------------------------------------------------------
    def quarantine(self, symbol: str, reason: str) -> None:
        """Stop serving a symbol for the rest of this service's cycle."""
        self._quarantined[symbol] = reason

    def is_quarantined(self, symbol: str) -> bool:
        return symbol in self._quarantined

    def _guard(self, symbol: str) -> None:
        reason = self._quarantined.get(symbol)
        if reason is not None:
            raise MarketDataUnavailable(f"quarantined:{reason}")

    # -- transport ---------------------------------------------------------
    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            body = self._transport(method, payload)
        except Exception as exc:  # noqa: BLE001 - any transport fault is unavailability
            raise MarketDataUnavailable(f"transport_error:{exc}") from exc
        if body is None:
            raise MarketDataUnavailable("transport_unavailable")
        if not isinstance(body, dict):
            raise MarketDataUnavailable("malformed_response")
        if body.get("error") is not None:
            raise MarketDataUnavailable("transport_error")
        return body

    def _check_feed(self, body: dict[str, Any], allow_feed_mismatch: bool) -> str:
        feed = body.get(_FEED_KEY)
        if not isinstance(feed, str) or not feed:
            raise MarketDataUnavailable("missing_feed")
        if feed != self._config.data_feed and not allow_feed_mismatch:
            raise MarketDataUnavailable("feed_mismatch")
        return feed

    # -- quotes ------------------------------------------------------------
    def quote(
        self,
        symbol: str,
        *,
        allow_stale: bool = False,
        allow_feed_mismatch: bool = False,
    ) -> Quote:
        """Top-of-book as of now; a stale or foreign-feed stamp refuses by default."""
        self._guard(symbol)
        body = self._call("quote", {"symbol": symbol})
        feed = self._check_feed(body, allow_feed_mismatch)
        as_of = _ts_of(body)
        bid = _num(body, "bid", required=False)
        ask = _num(body, "ask", required=False)
        last = _num(body, "last", required=False)
        if bid is None and ask is None and last is None:
            raise MarketDataUnavailable("missing_field:price")
        mid = None if bid is None or ask is None else (bid + ask) / 2.0
        spread_bps = None if mid is None or mid <= 0 else (ask - bid) / mid * 10_000.0
        age_s = (_as_utc(self._now()) - _as_utc(as_of)).total_seconds()
        quote = Quote(symbol, bid, ask, last, spread_bps, as_of, feed, age_s)
        if age_s > self._config.max_quote_staleness_s and not allow_stale:
            raise MarketDataUnavailable("stale_quote")
        return quote

    # -- bars --------------------------------------------------------------
    def bars(
        self,
        symbol: str,
        *,
        count: int,
        timeframe: str = "1Min",
        as_of: datetime | None = None,
        allow_stale: bool = False,
        allow_feed_mismatch: bool = False,
    ) -> tuple[Bar, ...]:
        """Intraday bars up to ``as_of`` (default: now), newest last."""
        return self._read_bars(
            symbol,
            count=count,
            timeframe=timeframe,
            as_of=as_of,
            allow_stale=allow_stale,
            allow_feed_mismatch=allow_feed_mismatch,
            staleness_s=self._config.max_bar_staleness_s,
        )

    def daily(
        self,
        symbol: str,
        *,
        count: int,
        as_of: datetime | None = None,
        allow_stale: bool = False,
        allow_feed_mismatch: bool = False,
    ) -> tuple[Bar, ...]:
        """Daily bars for baselines. Their age is a session or more by construction,
        so the intraday staleness budget does not apply; the live read does."""
        return self._read_bars(
            symbol,
            count=count,
            timeframe="1Day",
            as_of=as_of,
            allow_stale=allow_stale,
            allow_feed_mismatch=allow_feed_mismatch,
            staleness_s=None,
        )

    def _read_bars(
        self,
        symbol: str,
        *,
        count: int,
        timeframe: str,
        as_of: datetime | None,
        allow_stale: bool,
        allow_feed_mismatch: bool,
        staleness_s: float | None,
    ) -> tuple[Bar, ...]:
        self._guard(symbol)
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        cutoff = as_of if as_of is not None else self._now()
        body = self._call(
            "bars",
            {
                "symbol": symbol,
                "count": count,
                "timeframe": timeframe,
                "as_of": cutoff.isoformat(),
            },
        )
        self._check_feed(body, allow_feed_mismatch)
        raw_bars = body.get(_BARS_KEY)
        if not isinstance(raw_bars, list):
            raise MarketDataUnavailable("missing_field:bars")
        parsed = sorted((self._parse_bar(symbol, raw) for raw in raw_bars), key=lambda b: b.ts)
        # point-in-time: a bar the decision could not have had is never returned
        visible = tuple(bar for bar in parsed if bar.ts <= cutoff)
        if not visible:
            raise MarketDataUnavailable("all_bars_future")
        visible = visible[-count:]
        if staleness_s is not None:
            age_s = (_as_utc(cutoff) - _as_utc(visible[-1].ts)).total_seconds()
            if age_s > staleness_s and not allow_stale:
                raise MarketDataUnavailable("stale_bars")
        return visible

    @staticmethod
    def _parse_bar(symbol: str, raw: Any) -> Bar:
        if not isinstance(raw, dict):
            raise MarketDataUnavailable("malformed_bar")
        ts = _ts_of(raw)
        return Bar(
            symbol=symbol,
            ts=ts,
            open=_num(raw, "open", required=True),
            high=_num(raw, "high", required=True),
            low=_num(raw, "low", required=True),
            close=_num(raw, "close", required=True),
            volume=_num(raw, "volume", required=True),
        )
