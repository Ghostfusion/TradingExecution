"""Effectively-once ingest: the dedupe table in front of the pipeline (plan §2.5, C1).

``watch.py`` finds artifacts; the inbox decides whether this process may act on
one. Two properties matter and are both tested:

* **Effectively once** — the admission row is written *before* any side effect
  (own-before-write, design §13), so a crash between admit and emit leaves an
  ``admitted`` row that :meth:`Inbox.pending` surfaces for recovery instead of a
  silently replayed decision. A second admission of the same key is ``deduped``:
  one producer artifact, one effect.
* **Never silent** — an unreadable, non-compliant, hash-mismatched or expired
  artifact is dead-lettered with a machine-readable ``reason_code``; a
  produced-but-not-permissible signal is quarantined. Both are audited.

The key is stable across replays: ``service:run_id:artifact_sha256`` when the
producer supplies them, else the decision hash, else the canonical body hash.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import EnvelopeError, dead_letter, validate_envelope
from .schema import sha256_of
from .stores import _atomic_append

#: Admission kinds (closed set - the processor branches on these).
ACCEPTED = "accepted"
DEDUPED = "deduped"
DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True)
class Admission:
    """The verdict on one dropped artifact."""

    kind: str
    key: str
    path: Path
    raw: dict[str, Any] | None = None
    reason_code: str | None = None
    detail: str = ""

    @property
    def admitted(self) -> bool:
        return self.kind == ACCEPTED


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise EnvelopeError("not_an_object", f"{path.name} is not a JSON object")
    return raw


class Inbox:
    """Dedupe table + boundary validation in front of the processor."""

    def __init__(
        self,
        path: str | Path,
        dead_letter_dir: str | Path,
        quarantine_dir: str | Path,
        now: Callable[[], datetime],
        audit: Any | None = None,
    ) -> None:
        self.path = Path(path)
        self.dead_letter_dir = Path(dead_letter_dir)
        self.quarantine_dir = Path(quarantine_dir)
        self._now = now
        self._audit = audit

    # -- dedupe table ----------------------------------------------------
    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("key"):
                rows.append(row)
        return rows

    def state_of(self, key: str) -> str | None:
        """Latest recorded state for a key (``admitted`` | ``committed`` | None)."""
        state = None
        for row in self._rows():
            if row["key"] == key:
                state = str(row.get("state") or "admitted")
        return state

    def seen(self, key: str) -> bool:
        return self.state_of(key) is not None

    def pending(self) -> list[str]:
        """Keys admitted but never committed - re-driven on restart."""
        latest: dict[str, str] = {}
        for row in self._rows():
            latest[row["key"]] = str(row.get("state") or "admitted")
        return sorted(k for k, state in latest.items() if state == "admitted")

    def key_for(self, raw: dict[str, Any], path: Path) -> str:
        producer = raw.get("producer") if isinstance(raw.get("producer"), dict) else {}
        service = str(producer.get("service") or "").strip()
        run_id = str(producer.get("run_id") or "").strip()
        artifact = str(raw.get("artifact_sha256") or "").strip()
        if service and run_id and artifact:
            return f"{service}:{run_id}:{artifact.removeprefix('sha256:')}"
        decision = str(raw.get("decision_hash") or "").strip()
        if decision:
            return decision.removeprefix("sha256:")
        return sha256_of(raw)

    def _record(
        self, key: str, state: str, raw: dict[str, Any] | None, path: Path, **extra: Any
    ) -> None:
        body: dict[str, Any] = raw if raw is not None else {}
        row = {
            "key": key,
            "state": state,
            "at": self._now().isoformat(timespec="seconds"),
            "ticker": body.get("ticker"),
            "decision_hash": body.get("decision_hash"),
            "source": str(path),
        }
        row.update({k: v for k, v in extra.items() if v not in (None, "")})
        _atomic_append(self.path, json.dumps(row, sort_keys=True, default=str) + "\n")

    # -- ingest ----------------------------------------------------------
    def admit(self, path: str | Path, *, raw: dict[str, Any] | None = None) -> Admission:
        """Validate + dedupe one artifact. Never raises on bad input."""
        p = Path(path)
        now = self._now()
        body: dict[str, Any]
        if raw is None:
            try:
                body = _read_json(p)
            except (OSError, json.JSONDecodeError, EnvelopeError) as exc:
                return self._dead_letter(None, EnvelopeError("unreadable", str(exc)), p)
        else:
            body = raw

        try:
            validate_envelope(body, now=now)
        except EnvelopeError as exc:
            producer = body.get("producer") if isinstance(body.get("producer"), dict) else {}
            return self._dead_letter(
                body, exc, p, producer_hint=str(producer.get("service") or "") or None
            )

        key = self.key_for(body, p)
        if self.seen(key):
            self._audit_row("deduped", f"already admitted ({self.state_of(key)})", key, p)
            return Admission(DEDUPED, key, p, raw=body, detail="duplicate artifact")

        self._record(key, "admitted", body, p, schema_version=body.get("schema_version"))
        # distinct from the processor's own "accepted" row (which records the
        # verdict): the boundary records the *admission*, not the outcome
        self._audit_row("admitted", "admitted to the pipeline", key, p)
        return Admission(ACCEPTED, key, p, raw=body)

    def commit(self, key: str, *, signal_id: str = "") -> None:
        """Mark the side effect for ``key`` as done (idempotent)."""
        if self.state_of(key) == "committed":
            return
        self._record(key, "committed", None, self.path, signal_id=signal_id)

    # -- non-permissible signals -----------------------------------------
    def quarantine(
        self, envelope: dict[str, Any], reason_code: str, reason: str, *, key: str = ""
    ) -> Path:
        """Record a produced-but-not-permissible signal (design §2.5)."""
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%dT%H%M%S")
        ticker = str(envelope.get("ticker") or "unknown")
        payload = self.quarantine_dir / f"{ticker}-{stamp}-{reason_code}.json"
        payload.write_text(
            json.dumps(envelope, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        sidecar = {
            "reason_code": reason_code,
            "detail": reason,
            "received_at": self._now().isoformat(timespec="seconds"),
            "key": key or None,
        }
        (payload.with_suffix(".reason.json")).write_text(
            json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8"
        )
        self._audit_row("quarantined", reason, key, payload)
        return payload

    # -- internals -------------------------------------------------------
    def _dead_letter(
        self,
        body: dict[str, Any] | None,
        error: EnvelopeError,
        path: Path,
        producer_hint: str | None = None,
    ) -> Admission:
        result = dead_letter(
            body if body is not None else {"unreadable": str(path)},
            error,
            source=path,
            dir_path=self.dead_letter_dir,
            now=self._now(),
            producer_hint=producer_hint,
        )
        key = self.key_for(body, path) if body else f"unreadable:{path.name}"
        self._audit_row(
            "dead_lettered",
            error.detail,
            key,
            path,
            reason_code=error.reason_code,
            dead_letter=str(result.path),
        )
        return Admission(
            DEAD_LETTERED, key, path, raw=body, reason_code=error.reason_code, detail=error.detail
        )

    def _audit_row(self, kind: str, reason: str, key: str, path: Path, **extra: Any) -> None:
        if self._audit is not None:
            self._audit.append(kind, reason, key=key, path=str(path), **extra)
