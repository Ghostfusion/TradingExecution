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
from datetime import datetime, time
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
    return datetime.now()


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
    mode: str = "signal"
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

    # --- intraday sleeve -------------------------------------------------
    intraday_enabled: bool = False
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
    api_enabled: bool = False
    api_bind: str = "127.0.0.1:8787"
    api_key_id: str | None = None
    api_signing_secret: str | None = None
    api_replay_window_s: float = 300.0
    approval_ttl_s: float = 900.0
    mcp_enabled: bool = False
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
        key = next((p for p in _PREFIXES if k.startswith(p)), k)
        key = k[len(key):]
        key = key.lower()
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
