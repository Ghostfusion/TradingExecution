"""Watchdog: pages on a stale daemon heartbeat (plan §4.12 / onboarding).

The daemon touches ``heartbeat_path`` every poll cycle. This module checks
freshness and can dispatch a ``heartbeat_loss`` notifier event + exit non-zero
when the heartbeat is stale, so a separate supervised process can restart the
daemon. Phase A: heartbeat-only (no force-flatten — that arrives with M1
orders); the event is journal-first (the notifier never blocks).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .notifier import Notifier

DEFAULT_MAX_AGE_S = 120.0


def heartbeat_age_seconds(heartbeat_path: str | Path, now: float | None = None) -> float | None:
    """Age of the heartbeat file in seconds; None when missing/unreadable."""
    p = Path(heartbeat_path)
    if not p.exists():
        return None
    try:
        written = float(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    stamp = (time.time() if now is None else now)
    age = stamp - written
    return max(0.0, age)


def check_heartbeat(
    heartbeat_path: str | Path,
    notifier: Notifier,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    now: float | None = None,
    now_dt: datetime | None = None,
) -> bool:
    """Return True when the daemon heartbeat is fresh; dispatch on loss."""
    age = heartbeat_age_seconds(heartbeat_path, now)
    if age is None:
        detail = f"heartbeat missing at {Path(heartbeat_path)}"
    elif age <= max_age_s:
        return True
    else:
        detail = f"heartbeat stale: {age:.0f}s > {max_age_s:.0f}s max"
    if notifier.enabled:
        event = {
            "event": "heartbeat_loss",
            "ts": (now_dt or datetime.now()).isoformat(timespec="seconds"),
            "detail": detail,
            "max_age_s": max_age_s,
        }
        notifier.send(event)
    return False
