"""Kill switch + persisted HALT episode latch (plan §4.8).

A filesystem sentinel halts signal emission instantly. The HALT episode is
persisted beside the sentinel: after a restart during a halt, suppression for
that episode is replayed (never a fresh signal for a halted episode). Phase A
halts EMISSION; the sweeper (cancel/flatten) arrives with M1 orders.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def is_halted(sentinel_path: str | Path) -> bool:
    return Path(sentinel_path).exists()


def ensure_episode(latch_path: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """Read the current HALT episode or create it on first halt."""
    p = Path(latch_path)
    if p.exists():
        try:
            ep = json.loads(p.read_text(encoding="utf-8"))
            if ep.get("episode"):
                return ep
        except (json.JSONDecodeError, OSError):
            pass
    ep = {
        "episode": str(uuid.uuid4())[:8],
        "since": (now or datetime.now()).isoformat(timespec="seconds"),
        "reason": "kill_switch sentinel",
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(ep, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, p)
    return ep


def read_episode(latch_path: str | Path) -> dict[str, Any] | None:
    p = Path(latch_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def resume(sentinel_path: str | Path, latch_path: str | Path) -> None:
    """Clear the sentinel; keep the episode record for audit history."""
    sp = Path(sentinel_path)
    if sp.exists():
        sp.unlink(missing_ok=True)
    # episode latch intentionally retained as the audit record of the halt
