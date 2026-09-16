"""Typed daemon configuration with .env + environment override support.

Load order: built-in defaults -> .env file -> process env (``TRADINGEXEC_*``
and ``ALPACA_*``). Configuration is frozen after load and hashed so every
emitted envelope carries the exact config hash it was produced under
(design §8 / plan §4.1).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

ENV_FILE = "TRADINGEXEC_ENV_FILE"
_PREFIX = "TRADINGEXEC_"
_ALPACA_PREFIX = "ALPACA_"
# The research repo's TRADINGAGENTS_ALPACA_* names are deliberately NOT a
# supported input (design §2.2 rule 3): execution owns its keys, and accepting
# the sibling repo's variable names would make its .env part of this process.
_PREFIXES = (_PREFIX, _ALPACA_PREFIX)
# env var name -> Config field name (only where they differ)
# mapping is applied to the env var AFTER the known prefix is stripped:
# "ALPACA_API_KEY" -> "api_key", "TRADINGAGENTS_ALPACA_API_KEY_ID" -> "api_key_id"
_ALIASES = {
    "api_key": "alpaca_key",
    "api_key_id": "alpaca_key",
    "secret": "alpaca_secret",
    "secret_key": "alpaca_secret",
    "api_secret": "alpaca_secret",
}

#: Never hashed and never serialised (design §2.2 rule 3, plan §3).
_SECRET_FIELDS = frozenset({"alpaca_key", "alpaca_secret", "api_signing_secret"})


def now_utc() -> datetime:
    """The daemon clock: naive UTC, matching the envelope convention.

    Naive-UTC on purpose. `calendar.to_et`/`session_phase`, `alpaca_ref._parse_ts`
    and `stores._parse_ts` all read a naive stamp as UTC, so a host-local clock
    silently skewed every age computation by the host's offset - on the operator's
    US-Central box (2026-09-15) that was 5 h, which made a 5-hour-old quote look
    fresh and disabled the `market.quote_age_s` staleness check entirely. Keep
    this tz-free and UTC; never `datetime.now()`.
    """
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Config:
    watch_dir: Path = Path("./decisions")
    data_dir: Path = Path("./signals")
    audit_file: Path = Path("./audit/audit.jsonl")
    journal_file: Path = Path("./audit/journal.jsonl")
    mandate_path: Path = Path("./mandate.json")
    kill_switch_path: Path = Path("./kill_switch")
    halt_latch_path: Path = Path("./audit/halt_episode.json")
    heartbeat_path: Path = Path("./audit/heartbeat")
    pid_file: Path = Path("./signald.pid")

    poll_seconds: float = 10.0
    watch_recursive: bool = True
    latest_only: bool = True
    ingest_window_hours: float = 24.0
    cooldown_hours: float = 12.0
    min_cash_reserve_usd: float = 25000.0
    ref_required: bool = True
    approval_threshold_factor: float = 1.0

    notifier_url: str | None = None
    notifier_timeout_s: float = 5.0
    notifier_retries: int = 2

    alpaca_key: str | None = None
    alpaca_secret: str | None = None
    alpaca_paper: bool = True

    dry_run: bool = False

    # --- mode + sleeves (implementation plan §3) -------------------------
    # signal = no order path exists in this process; paper|live enable the
    # order path behind --execute; live additionally requires two opt-ins.
    # Owner decision (2026-09-13): every built switch ships ON. The default is
    # paper, so the gate, the sizer and the OrderGuard judge every candidate;
    # `execute=True` (CLI `--execute`) stays an INDEPENDENT opt-in before any
    # order is sent, and live still needs its second acknowledgement.
    mode: str = "paper"
    sleeve_swing_capital_pct: float = 0.70
    sleeve_intraday_capital_pct: float = 0.30
    sleeve_swing_vol_target: float = 0.10
    sleeve_intraday_vol_target: float = 0.10
    intraday_reserve_to_swing: bool = False

    # --- risk budgets (design §10 table) --------------------------------
    risk_per_trade_intraday_pct: float = 0.0025
    risk_per_trade_swing_pct: float = 0.005
    max_heat_pct: float = 0.03
    max_positions_intraday: int = 5
    max_positions_swing: int = 8
    max_single_name_pct_swing: float = 0.10
    max_single_name_pct_intraday: float = 0.05
    max_adv_pct: float = 0.25
    cluster_cap_pct: float = 0.25
    es_budget_sleeve_pct: float = 0.015
    es_budget_house_pct: float = 0.010
    es_confidence: float = 0.975
    es_backtest_days: int = 500
    daily_loss_soft_pct: float = 0.01
    daily_loss_hard_pct: float = 0.03
    derisk_5d_pct: float = 0.06
    derisk_dd_pct: float = 0.10
    derisk_halt_dd_pct: float = 0.15
    kelly_fraction: float = 0.25
    leverage_cap: float = 1.5
    vol_halflife_days: float = 20.0
    vol_warmup_days: int = 270
    vol_scale_cap: float = 1.5
    vol_rebalance_band: float = 0.12
    stress_correlation: float = 0.85

    # --- scan window (the reports poll) ----------------------------------
    # ON by default: the daemon only *scans* during the regular session, so an
    # after-hours report waits for the next open instead of paging a "next
    # session" card at 23:00. The session is an exchange fact, evaluated in ET
    # (09:30-16:00, 13:00 on half days) - on a US-Central host that is
    # 08:30-15:00 local, and the host clock's zone never enters the decision.
    # `signald run --once` deliberately ignores this (operator override).
    scan_rth_only: bool = True
    # Cross-check the calendar against the broker's own clock, which knows about
    # unscheduled halts and early closes the code-shipped calendar cannot.
    # Unavailable clock => do not scan (fail closed); set False to run offline.
    scan_confirm_broker_clock: bool = True
    # --- intraday sleeve -------------------------------------------------
    # ON by default (owner decision 2026-09-13). This is one of the two
    # opt-ins `route_intraday` requires - the caller still has to pass
    # `enabled=True` - and the session engine still needs `execute=True`.
    intraday_enabled: bool = True
    intraday_entry_after: str = "09:35"
    intraday_entry_before: str = "11:00"
    intraday_flat_by: str = "15:50"
    intraday_max_trades_per_name: int = 1
    opening_range_minutes: int = 5
    rvol_min: float = 2.0
    rvol_top_n: int = 20
    min_price: float = 5.0
    min_adv_shares: int = 1_000_000
    min_atr: float = 0.50
    adx_trend: float = 25.0
    adx_range: float = 20.0
    stop_atr_mult: float = 1.75
    stop_mae_pctl: float = 0.80
    cost_gate_multiple: float = 3.0
    cost_liquid_bps: float = 12.0
    cost_lowfloat_bps: float = 30.0

    # --- data, clock -----------------------------------------------------
    data_feed: str = "sip"
    max_quote_staleness_s: float = 2.0
    max_bar_staleness_s: float = 60.0
    clock_max_offset_ms: float = 50.0

    # --- control API / MCP ----------------------------------------------
    # ON by default (owner decision 2026-09-13). `signald api` / `signald mcp`
    # (`signald/control.py`) serve them; the API additionally needs the key id
    # and signing secret below, and both refuse a non-loopback bind before any
    # socket exists. MCP mutating tools are absent unless listed in
    # `mcp_toolsets`. A patch flag is still not a listener: nothing is bound
    # until one of those commands runs.
    api_enabled: bool = True
    api_bind: str = "127.0.0.1:8787"
    api_key_id: str | None = None
    api_signing_secret: str | None = None
    api_replay_window_s: float = 300.0
    approval_ttl_s: float = 900.0
    mcp_enabled: bool = True
    mcp_bind: str = "127.0.0.1"
    mcp_toolsets: str = "read,simulate,propose"

    # --- lineage ---------------------------------------------------------
    trial_registry: Path = Path("./audit/trials.jsonl")
    backup_dir: Path = Path("./backups")

    # Testability seams (never hashed / never serialised).
    now_fn: Callable[[], datetime] = None  # type: ignore[assignment]
    transport: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "now_fn", self.now_fn or now_utc)
        for f in fields(self):
            # any field whose default is a Path is coerced (never a hardcoded list)
            if isinstance(f.default, Path):
                object.__setattr__(self, f.name, _as_path(getattr(self, f.name)))
        self._validate()

    def _validate(self) -> None:
        """A misconfigured process must not start (fail closed at load)."""
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode={self.mode!r} is not one of {list(VALID_MODES)}")
        ceilings = (self.sleeve_swing_capital_pct, self.sleeve_intraday_capital_pct)
        if any(not 0.0 <= c <= 1.0 for c in ceilings) or sum(ceilings) > 1.0:
            raise ValueError(
                f"sleeve capital ceilings must be fractions summing to <=1: {ceilings}"
            )
        for key in ("intraday_entry_after", "intraday_entry_before", "intraday_flat_by"):
            parse_hhmm(getattr(self, key))
        if not 0.0 < self.stress_correlation < 1.0:
            raise ValueError(f"stress_correlation must be in (0,1): {self.stress_correlation}")

    @property
    def order_path_enabled(self) -> bool:
        """The order path exists only in paper/live mode (plan §1.1)."""
        return self.mode in {"paper", "live"}

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def now(self) -> datetime:
        return self.now_fn()

    def config_hash(self) -> str:
        """Deterministic hash of every non-secret, non-seam field."""
        payload = {}
        for f in fields(self):
            if f.name in {"now_fn", "transport"} or f.name in _SECRET_FIELDS:
                continue
            v = getattr(self, f.name)
            payload[f.name] = str(v) if isinstance(v, Path) else v
        data = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(data).hexdigest()[:16]


def _as_path(v: Any) -> Path:
    return Path(v) if not isinstance(v, Path) else v


#: The only modes this process understands (plan §3). `signal` has no order path.
VALID_MODES = ("signal", "paper", "live")


def parse_hhmm(text: str) -> time:
    """Parse ``HH:MM`` (ET session times). Raises ValueError - a typo fails at load.

    Reused by the intraday time gates so a session boundary is parsed once.
    """
    raw = str(text or "").strip()
    try:
        hour, _, minute = raw.partition(":")
        return time(int(hour), int(minute))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a HH:MM session time: {text!r}") from exc


def _parse_bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


def load_config(
    env_file: str | Path | None = None,
    environ: dict[str, str] | None = None,
    **overrides: Any,
) -> Config:
    """Build a Config from defaults + .env + environment (+ keyword overrides).

    ``environ`` defaults to ``os.environ``; pass a dict in tests to isolate.
    Keyword overrides win over everything (used by the CLI).
    """
    environ = dict(os.environ if environ is None else environ)

    values: dict[str, Any] = {}
    # 1. .env file (explicit path, or TRADINGEXEC_ENV_FILE, or ./ .env)
    candidates: list[Path] = []
    if env_file is not None:
        candidates.append(_as_path(env_file))
    elif environ.get(ENV_FILE):
        candidates.append(_as_path(environ[ENV_FILE]))
    else:
        candidates.append(Path(".env"))
    for cand in candidates:
        if cand.exists():
            for line in cand.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()

    # 2. real env overrides .env values
    for k, v in environ.items():
        if k.startswith(_PREFIXES):
            values[k] = v

    # 3. map flat keys onto dataclass fields
    field_names = {f.name for f in fields(Config)}
    mapped: dict[str, Any] = {}
    for k, v in values.items():
        prefix = next((p for p in _PREFIXES if k.startswith(p)), None)
        if prefix is None:
            continue  # only our own prefixes are read (design §2.2 rule 3)
        key = k[len(prefix):].lower()
        # The alias table translates *Alpaca's* env names onto our fields, so it
        # applies to the ALPACA_ prefix only: TRADINGEXEC_API_KEY_ID is the
        # control API's key id, not ALPACA_API_KEY_ID (which is the Alpaca one).
        if prefix == _ALPACA_PREFIX:
            key = _ALIASES.get(key, key)
        if key not in field_names:
            continue
        f = next(x for x in fields(Config) if x.name == key)
        t = f.type if not isinstance(f.type, str) else f.type
        if t in ("bool", bool):
            mapped[key] = _parse_bool(v)
        elif t in ("int", int):
            mapped[key] = int(v)
        elif t in ("float", float):
            mapped[key] = float(v)
        elif t in ("str", str):
            mapped[key] = v
        else:  # Path (and any optional union -> raw value)
            mapped[key] = v

    mapped.update({k: v for k, v in overrides.items() if v is not None})
    return Config(**mapped)
