"""Notifier — webhook dispatcher (plan §4.5).

Events: ``signal`` (every emitted signal), ``error`` (daemon/processing
failures), ``kill_switch``, ``heartbeat_loss`` (watchdog pages on stale
heartbeat). Dispatch is best-effort and NEVER blocks signal persistence:
stores/journal are written first, then notifier runs with timeout + retry, and
its outcome is recorded in the audit ledger. The transport is injectable for
hermetic tests.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

WebhookTransport = Callable[[dict[str, Any]], None]


class Notifier:
    def __init__(
        self,
        url: str | None = None,
        timeout_s: float = 5.0,
        retries: int = 2,
        transport: WebhookTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.retries = max(0, int(retries))
        self._transport = transport
        self._now = now or datetime.now

    @property
    def enabled(self) -> bool:
        return bool(self.url or self._transport)

    def send(self, event: dict[str, Any]) -> bool:
        """Dispatch best-effort; returns True on success. Never raises."""
        if not self.enabled:
            return True  # no sink configured: nothing to fail
        payload = json.dumps(event).encode("utf-8")
        for attempt in range(self.retries + 1):
            try:
                self._deliver(payload)
                return True
            except Exception:  # noqa: BLE001 - notifier must never raise
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        return False

    def _deliver(self, payload: bytes) -> None:
        if self._transport is not None:
            self._transport(json.loads(payload.decode("utf-8")))
            return
        if not self.url:
            raise RuntimeError("no notifier URL configured")
        req = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            resp.read()

    def signal_event(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return {"event": "signal", "ts": self._now().isoformat(timespec="seconds"),
                "signal_id": envelope["signal_id"], "envelope": envelope}

    def error_event(self, source: str, detail: str) -> dict[str, Any]:
        return {"event": "error", "ts": self._now().isoformat(timespec="seconds"),
                "source": source, "detail": detail}

    def halt_event(self, episode: str, reason: str) -> dict[str, Any]:
        return {"event": "kill_switch", "ts": self._now().isoformat(timespec="seconds"),
                "episode": episode, "reason": reason}
