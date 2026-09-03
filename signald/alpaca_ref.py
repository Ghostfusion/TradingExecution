"""Reference data from Alpaca paper (Phase A: reads only, never submits orders).

Every outbound call goes through an injectable ``transport`` seam so the full
pipeline runs hermetic (zero network) in tests — the same pattern QSE uses for
its execution handler. The real path uses ``alpaca-py``.

Phase A reality notes (from Alpaca docs): paper-only accounts get IEX data;
the free tier withholds ~15 min of recent SIP — reference timestamps are
surfaced and a ``stale`` flag is set when the quote is older than
``staleness_threshold_s`` (default 15 min). No order path exists here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

STALENESS_THRESHOLD_S = 15 * 60


class ReferenceUnavailable(Exception):
    """Reference data could not be produced (fail closed upstream)."""


@dataclass(frozen=True)
class RefData:
    last: float | None = None
    vwap: float | None = None
    spread_usd: float | None = None
    ts: datetime | None = None
    feed: str | None = None
    stale: bool = False
    cash: float | None = None
    buying_power: float | None = None
    equity: float | None = None
    market_open: bool | None = None
    asset_tradable: bool | None = None
    positions_value: float | None = None

    @property
    def complete_for_gates(self) -> bool:
        """Fail-closed reference: cash + asset state must be present."""
        return None not in (self.cash, self.asset_tradable)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "last": self.last,
            "vwap": self.vwap,
            "spread_usd": self.spread_usd,
            "ts": self.ts.isoformat(timespec="seconds") if self.ts else None,
            "feed": self.feed,
            "stale": self.stale,
            "cash": self.cash,
            "buying_power": self.buying_power,
            "equity": self.equity,
            "market_open": self.market_open,
            "asset_tradable": self.asset_tradable,
            "positions_value": self.positions_value,
        }
        return d


# Seam contract: transport(method: str, ticker: str) -> dict | None.
# Supported methods: quote, account, clock, asset, positions.
Transport = Callable[[str, str], dict[str, Any] | None]


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class AlpacaReference:
    """Reference reads for one ticker: quote + account + clock + asset + positions."""

    def __init__(
        self,
        api_key: str | None = None,
        secret: str | None = None,
        paper: bool = True,
        transport: Transport | None = None,
        staleness_threshold_s: float = STALENESS_THRESHOLD_S,
    ) -> None:
        self._key = api_key
        self._secret = secret
        self._paper = paper
        self._transport = transport
        self._staleness = staleness_threshold_s
        self._clients = None  # lazy alpaca-py clients

    def _lazy_clients(self) -> tuple[Any, Any]:
        if self._clients is None:
            if self._transport is not None:
                raise ReferenceUnavailable("transport seam active; no alpaca-py clients")
            if not self._key or not self._secret:
                raise ReferenceUnavailable("Alpaca paper credentials not configured")
            try:
                from alpaca.data.historical import (
                    StockHistoricalDataClient,  # type: ignore[import-not-found]
                )
                from alpaca.trading.client import TradingClient  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - env dependency
                raise ReferenceUnavailable(f"alpaca-py not installed: {exc}") from exc
            self._clients = (
                StockHistoricalDataClient(self._key, self._secret),
                TradingClient(self._key, self._secret, paper=self._paper),
            )
        return self._clients

    def _call(self, method: str, ticker: str) -> dict[str, Any]:
        if self._transport is not None:
            out = self._transport(method, ticker)
            if out is None:
                raise ReferenceUnavailable(f"reference method {method} unavailable")
            return out
        return self._real(method, ticker)

    def _real(self, method: str, ticker: str) -> dict[str, Any]:
        data_client, trade_client = self._lazy_clients()
        try:
            if method == "quote":
                from alpaca.data.requests import (
                    StockLatestQuoteRequest,  # type: ignore[import-not-found]
                )

                q = data_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=[ticker])
                )
                row = q.get(ticker, None)
                if row is None:
                    return {}
                ts = getattr(row, "timestamp", None)
                return {
                    "last": _num(getattr(row, "bid", None) or getattr(row, "ask", None)),
                    "vwap": None,
                    "spread_usd": _spread(row),
                    "ts": ts.isoformat() if ts else None,
                    "feed": "iex",
                }
            if method == "account":
                acct = trade_client.get_account()
                return {"cash": _num(acct.cash), "buying_power": _num(acct.buying_power),
                        "equity": _num(acct.equity)}
            if method == "clock":
                clock = trade_client.get_clock()
                return {"is_open": bool(getattr(clock, "is_open", None))}
            if method == "asset":
                asset = trade_client.get_asset(ticker)
                return {"tradable": bool(getattr(asset, "tradable", None))}
            if method == "positions":
                pos = trade_client.get_all_positions()
                return {"positions_value": round(sum(_num(p.market_value) or 0.0 for p in pos), 2)}
        except Exception as exc:  # noqa: BLE001 - any broker error is a failure path
            raise ReferenceUnavailable(f"alpaca {method} failed: {exc}") from exc
        raise ReferenceUnavailable(f"unknown reference method: {method}")

    def snapshot(self, ticker: str) -> RefData:
        """Fetch all reference inputs for a ticker; raises ReferenceUnavailable on any failure."""
        quote = self._call("quote", ticker)
        account = self._call("account", ticker)
        clock = self._call("clock", ticker)
        asset = self._call("asset", ticker)
        positions = self._call("positions", ticker)

        ts = _parse_ts(quote.get("ts"))
        stale = False
        if ts is not None:
            age_s = (now_utc() - ts).total_seconds()
            stale = age_s > self._staleness
        return RefData(
            last=_num(quote.get("last")),
            vwap=_num(quote.get("vwap")),
            spread_usd=_num(quote.get("spread_usd")),
            ts=ts,
            feed=quote.get("feed"),
            stale=stale,
            cash=_num(account.get("cash")),
            buying_power=_num(account.get("buying_power")),
            equity=_num(account.get("equity")),
            market_open=bool(clock.get("is_open")) if clock.get("is_open") is not None else None,
            asset_tradable=(
                bool(asset.get("tradable")) if asset.get("tradable") is not None else None
            ),
            positions_value=_num(positions.get("positions_value")),
        )


def _spread(row: Any) -> float | None:
    bid = getattr(row, "bid", None)
    ask = getattr(row, "ask", None)
    if bid is None or ask is None:
        return None
    try:
        return round(float(ask) - float(bid), 4)
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def now_utc() -> datetime:
    # naive UTC to stay comparable with the daemon clock (both tz-free)
    return datetime.now(UTC).replace(tzinfo=None)
