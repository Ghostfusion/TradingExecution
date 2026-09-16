"""Watch loop: poll the decisions inbox and feed artifacts to the processor.

``discover()`` handles the two watch shapes:

- **reports mode** (``watch_recursive=True``, the TradingAgents ``reports/``
  tree): finds ``research_decision.json`` files recursively (run_card.json and
  other JSON are ignored) and — when ``latest_only=True`` — keeps only the
  NEWEST decision per ticker (by file mtime), so a symbol's older runs never
  produce signals; a blocked/killed newest stays the only candidate.
- **inbox mode** (``watch_recursive=False``): flat ``*.json`` files in the
  watch dir, processed as-is.

``run_forever`` additionally honours the **scan window**: with
``config.scan_rth_only`` (the default) it only scans while the regular US
session is open — 09:30–16:00 ET on trading days, 13:00 on half days — so an
after-hours report waits for the next open instead of paging a "next session"
card at midnight. ``run_once`` deliberately ignores the window: ``--once`` is
the operator's override.

The idempotency journal still skips repeated paths (same decision_hash).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .daemon import touch_heartbeat
from .marketdata.calendar import session_for, session_phase, to_et
from .processor import ProcessResult, SignalProcessor

ARTIFACT_NAME = "research_decision.json"


@dataclass(frozen=True)
class ScanWindow:
    """Whether the daemon may scan now, and why (printed on transition only)."""

    open: bool
    reason: str
    detail: str


def _ticker_of(path: Path) -> str | None:
    """Ticker from the artifact's own field, else the folder-name prefix."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        t = str(doc.get("ticker") or "").strip().upper()
        if t:
            return t
    except (json.JSONDecodeError, OSError):
        pass
    head = path.parent.name.split("_")[0].strip().upper()
    return head or None


class WatchLoop:
    def __init__(self, processor: SignalProcessor, poll_seconds: float = 10.0) -> None:
        self.processor = processor
        self.poll_seconds = max(0.5, float(poll_seconds))

    def discover(self) -> list[Path]:
        watch = Path(self.processor.cfg.watch_dir)
        if not watch.exists():
            return []
        if self.processor.cfg.watch_recursive:
            files = list(watch.rglob(ARTIFACT_NAME))
            if not self.processor.cfg.latest_only:
                return sorted(files)
            best: dict[str, tuple[float, Path]] = {}
            for f in files:
                ticker = _ticker_of(f)
                if not ticker:
                    continue
                mtime = f.stat().st_mtime
                if ticker not in best or mtime > best[ticker][0]:
                    best[ticker] = (mtime, f)
            return [v[1] for v in best.values()]
        return sorted(
            p for p in watch.iterdir()
            if p.is_file() and p.name.endswith(".json")
        )

    def run_once(self) -> list[ProcessResult]:
        # The processor holds one Mandate for its whole life, so re-read the file
        # each cycle: an operator's `signald mandate-add` then takes effect on the
        # next poll instead of on a restart.
        self.processor.refresh_mandate()
        results: list[ProcessResult] = []
        for artifact in self.discover():
            results.append(self.processor.process(artifact))
        return results

    def scan_window(self) -> ScanWindow:
        """Regular-session gate for the poll loop, evaluated on the exchange clock.

        The stamp follows the repo convention - naive means UTC - so a tz-free
        daemon clock and an aware injected clock both land on the same ET phase.
        The session itself is an ET fact (09:30-16:00, 13:00 on half days), so the
        host clock's zone never enters the decision: on this US-Central box the
        regular session is 08:30-15:00 local.
        """
        cfg = self.processor.cfg
        stamp = cfg.now()
        et = to_et(stamp)
        where = f"{et:%Y-%m-%d %H:%M} ET"
        if not cfg.scan_rth_only:
            return ScanWindow(True, "any", f"scan window disabled ({where})")
        phase = session_phase(stamp)
        if phase != "rth":
            return ScanWindow(False, phase, f"market {phase} ({where}); not scanning")
        if cfg.scan_confirm_broker_clock:
            broker = self.processor.ref.clock_is_open()
            if broker is None:
                return ScanWindow(False, "broker_unknown",
                                  f"broker clock unavailable ({where}); not scanning")
            if not broker:
                return ScanWindow(False, "broker_closed",
                                  f"broker clock says closed ({where}); not scanning")
        half = ", half day (13:00 close)" if session_for(et.date()).is_half_day else ""
        return ScanWindow(True, "rth", f"regular session ({where}{half})")

    def run_forever(self, stop: Callable[[], bool] | None = None, on_result=None) -> None:
        """Poll until stopped. Owns the cadence, the heartbeat and the scan window.

        The heartbeat is touched **before** the window check on purpose: a closed
        market must not look like a dead daemon to ``signald watchdog``.
        """
        last_reason: str | None = None
        while True:
            if stop is not None and stop():
                return
            touch_heartbeat(self.processor.cfg.heartbeat_path)
            window = self.scan_window()
            if window.open:
                if last_reason is not None:  # announce the reopen, once
                    print(f"[scan] {window.detail}", flush=True)
                    last_reason = None
                for res in self.run_once():
                    if on_result is not None:
                        on_result(res)
            elif window.reason != last_reason:  # announce each closed phase, once
                last_reason = window.reason
                print(f"[scan] {window.detail}", flush=True)
            time.sleep(self.poll_seconds)
