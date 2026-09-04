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
from datetime import datetime
from pathlib import Path
from typing import Any

ENV_FILE = "TRADINGEXEC_ENV_FILE"
_PREFIX = "TRADINGEXEC_"
_ALPACA_PREFIX = "ALPACA_"
# TradingAgents research repo uses TRADINGAGENTS_ALPACA_* for the same keys.
_TAGENT_ALPACA_PREFIX = "TRADINGAGENTS_ALPACA_"
_PREFIXES = (_TAGENT_ALPACA_PREFIX, _PREFIX, _ALPACA_PREFIX)
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

    # Testability seams (never hashed / never serialised).
    now_fn: Callable[[], datetime] = None  # type: ignore[assignment]
    transport: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "now_fn", self.now_fn or now_utc)
        for f in fields(self):
            if f.name in {"watch_dir", "data_dir", "audit_file", "journal_file",
                          "mandate_path", "kill_switch_path", "halt_latch_path",
                          "heartbeat_path", "pid_file"}:
                v = getattr(self, f.name)
                object.__setattr__(self, f.name, _as_path(v))

    def now(self) -> datetime:
        return self.now_fn()

    def config_hash(self) -> str:
        """Deterministic hash of every non-secret, non-seam field."""
        payload = {}
        for f in fields(self):
            if f.name in {"now_fn", "transport", "alpaca_key", "alpaca_secret"}:
                continue
            v = getattr(self, f.name)
            payload[f.name] = str(v) if isinstance(v, Path) else v
        data = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(data).hexdigest()[:16]


def _as_path(v: Any) -> Path:
    return Path(v) if not isinstance(v, Path) else v


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
