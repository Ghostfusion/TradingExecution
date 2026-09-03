"""Durable stores: signal feed, idempotency journal, hash-chained audit ledger.

Design (plan §4.5/§4.7): ``signals.jsonl`` is the canonical append-only feed,
``latest.json`` holds the last envelope per ticker, the journal records every
processed decision_hash (idempotency) plus emitted signals (daily caps,
cooldown), and the audit ledger is SHA-256-chained — a single edit breaks every
subsequent link and ``verify`` reports the first bad index.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

_suppress = suppress


def _atomic_append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(path, "ab") as live:
            live.write(Path(tmp).read_bytes())
            live.flush()
            os.fsync(live.fileno())
    finally:
        with _suppress(OSError):
            os.unlink(tmp)


class SignalStore:
    """Canonical signal feed: append-only jsonl + per-ticker latest."""

    def __init__(self, signals_path: str | Path, latest_path: str | Path) -> None:
        self.signals_path = Path(signals_path)
        self.latest_path = Path(latest_path)

    def append(self, envelope: dict[str, Any]) -> None:
        line = json.dumps(envelope, sort_keys=True) + "\n"
        _atomic_append(self.signals_path, line)

    def write_latest(self, envelope: dict[str, Any]) -> None:
        latest: dict[str, Any] = {}
        if self.latest_path.exists():
            try:
                latest = json.loads(self.latest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                latest = {}
        latest[envelope["ticker"]] = envelope
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.latest_path.with_name(self.latest_path.name + ".tmp")
        tmp.write_text(json.dumps(latest, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.latest_path)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.signals_path.exists():
            return []
        rows = []
        for line in self.signals_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


class Journal:
    """Processed-decision idempotency + signal bookkeeping."""

    def __init__(self, path: str | Path, now: Callable[[], datetime]) -> None:
        self.path = Path(path)
        self._now = now

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _append(self, row: dict[str, Any]) -> None:
        _atomic_append(self.path, json.dumps(row, sort_keys=True) + "\n")

    def mark_processed(self, decision_hash: str, ticker: str, path: str) -> None:
        self._append({
            "type": "processed",
            "decision_hash": decision_hash,
            "ticker": ticker,
            "path": path,
            "at": self._now().isoformat(timespec="seconds"),
        })

    def is_processed(self, decision_hash: str) -> bool:
        return any(r.get("type") == "processed" and r.get("decision_hash") == decision_hash
                   for r in self._rows())

    def add_signal(self, envelope: dict[str, Any]) -> None:
        self._append({
            "type": "signal",
            "signal_id": envelope["signal_id"],
            "decision_hash": envelope["decision_hash"],
            "ticker": envelope["ticker"],
            "action": envelope["action"],
            "emitted_at": envelope["emitted_at"],
            "notional_usd": envelope.get("gates", {}).get("target_notional_usd"),
        })

    def signals_today_count(self, now: datetime | None = None) -> int:
        today = (now or self._now()).date()
        out = 0
        for r in self._rows():
            if r.get("type") != "signal":
                continue
            t = _parse_ts(r.get("emitted_at"))
            if t is not None and t.date() == today:
                out += 1
        return out

    def last_signal(self) -> dict[str, Any] | None:
        for r in reversed(self._rows()):
            if r.get("type") == "signal":
                return r
        return None

    def replay(self) -> list[dict[str, Any]]:
        return self._rows()


class AuditChain:
    """SHA-256-chained append-only audit ledger with ``verify``.

    Each row pins the previous row's hash: ``prev_hash`` and carries its own
    ``hash = sha256(prev_hash + canonical row)``. Any edit breaks every
    subsequent link; ``verify`` walks the chain and reports the first bad
    index.
    """

    def __init__(self, path: str | Path, now: Callable[[], datetime]) -> None:
        self.path = Path(path)
        self._now = now

    def append(self, kind: str, reason: str, **data: Any) -> dict[str, Any]:
        prev_hash = "0" * 64
        rows = self.read()
        if rows:
            prev_hash = rows[-1]["hash"]
        row = {
            "index": len(rows),
            "ts": self._now().isoformat(timespec="seconds"),
            "kind": kind,
            "reason": reason,
            "data": data,
            "prev_hash": prev_hash,
        }
        body = json.dumps(
            {k: v for k, v in row.items() if k != "hash"}, sort_keys=True, default=str
        )
        row["hash"] = hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()
        _atomic_append(self.path, json.dumps(row, sort_keys=True) + "\n")
        return row

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({})  # corrupt line -> treated as bad row by verify
        return rows

    def verify(self, path: str | Path | None = None) -> tuple[bool, int, str]:
        """Return (ok, first_bad_index_or_-1, detail). Empty ledger verifies."""
        rows = self.read() if path is None else AuditChain(path, self._now).read()
        if not rows:
            return True, -1, "empty ledger"
        prev = "0" * 64
        for i, row in enumerate(rows):
            declared = row.get("hash")
            body = json.dumps(
            {k: v for k, v in row.items() if k != "hash"}, sort_keys=True, default=str
        )
            expect = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
            if declared != expect or row.get("prev_hash") != prev or not row.get("kind"):
                return False, i, f"row {i} hash/prev mismatch (declared {declared!r} vs {expect!r})"
            prev = declared or ""
        return True, -1, f"{len(rows)} rows chained"


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None
