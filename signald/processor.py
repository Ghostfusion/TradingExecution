"""The Phase A processing pipeline (plan §4.2).

research_decision.json -> load/validate/hash -> normalize -> fail-closed gates
-> envelope + reference + expected-cost band -> journal + signal store + audit
+ notifier. Any gate failure means NO signal; the rejection is audited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .alpaca_ref import AlpacaReference, RefData, ReferenceUnavailable
from .config import Config
from .gates import GateResult, evaluate
from .kill_switch import ensure_episode, is_halted, read_episode
from .mandate import Mandate
from .notifier import Notifier
from .schema import ContractError, build_signal_contract, parse_research_decision
from .stores import AuditChain, Journal, SignalStore


@dataclass(frozen=True)
class ProcessResult:
    kind: str  # emitted | dry_run | blocked | halted | skipped_duplicate | invalid
    envelope: dict[str, Any] | None = None
    reasons: tuple[str, ...] = ()


class SignalProcessor:
    def __init__(
        self,
        config: Config,
        mandate: Mandate,
        store: SignalStore,
        journal: Journal,
        audit: AuditChain,
        reference: AlpacaReference,
        notifier: Notifier,
    ) -> None:
        self.cfg = config
        self.mandate = mandate
        self.store = store
        self.journal = journal
        self.audit = audit
        self.ref = reference
        self.notifier = notifier

    def process(self, path: str | Path) -> ProcessResult:
        p = Path(path)
        now = self.cfg.now()

        if self.mandate.is_expired(now):
            return self._halted("mandate expired", p)

        if is_halted(self.cfg.kill_switch_path):
            ep = ensure_episode(self.cfg.halt_latch_path, now)
            return self._halted(f"kill switch {ep['episode']}", p)

        # 1. load + validate
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            rd = parse_research_decision(raw)
        except (json.JSONDecodeError, OSError, ContractError) as exc:
            self.audit.append("rejected_invalid", f"{p.name}: {exc}", path=str(p))
            return ProcessResult("invalid", reasons=(str(exc),))

        # 2. prechecks: future date + ingest window
        if rd.effective_date > now.date():
            self.audit.append("rejected", "effective_date in the future (no lookahead)",
                              ticker=rd.ticker, path=str(p))
            return ProcessResult("blocked", reasons=("effective_date in the future",))

        # 3. idempotency
        if self.journal.is_processed(rd.decision_hash):
            self.audit.append("skipped_duplicate", "decision_hash already processed",
                              ticker=rd.ticker, decision_hash=rd.decision_hash)
            return ProcessResult("skipped_duplicate")

        # 4. reference data (fail-closed when unavailable)
        try:
            ref = self.ref.snapshot(rd.ticker)
        except ReferenceUnavailable as exc:
            if self.cfg.ref_required:
                self.audit.append("rejected", f"reference unavailable: {exc}",
                                  ticker=rd.ticker, path=str(p))
                return ProcessResult("blocked", reasons=(f"reference unavailable: {exc}",))
            ref = RefData()
            self.audit.append("warn", f"reference unavailable (ref_required=false): {exc}",
                              ticker=rd.ticker)

        # 5. normalize -> contract
        contract = build_signal_contract(rd, self.mandate.expires.isoformat(), now, ref.equity)

        # 6. gates
        journal_state = {
            "signals_today": self.journal.signals_today_count(now),
            "last_signal": self.journal.last_signal(),
            "cooldown_hours": self.cfg.cooldown_hours,
            "ingest_window_hours": self.cfg.ingest_window_hours,
            "approval_threshold_factor": self.cfg.approval_threshold_factor,
        }
        gate: GateResult = evaluate(rd, contract, self.mandate, ref, journal_state, now)

        if gate.verdict == "BLOCK":
            self.audit.append(
                "rejected",
                "; ".join(gate.blocked),
                ticker=rd.ticker, decision_hash=rd.decision_hash,
                gate_reasons=list(gate.reasons),
            )
            return ProcessResult("blocked", reasons=gate.blocked)

        # 7. envelope
        seq = len(self.store.read_all()) + 1
        envelope = self._build_envelope(rd, contract, gate, ref, seq, now)

        # 8. persist + notify
        if self.cfg.dry_run:
            self.audit.append("dry_run", "signal computed; not persisted (dry-run)",
                              ticker=rd.ticker, decision_hash=rd.decision_hash,
                              gate_reasons=list(gate.reasons))
            return ProcessResult("dry_run", envelope=envelope, reasons=gate.reasons)

        self.journal.mark_processed(rd.decision_hash, rd.ticker, str(p))
        self.journal.add_signal(envelope)
        self.store.append(envelope)
        self.store.write_latest(envelope)
        self.audit.append("accepted", "; ".join(gate.reasons),
                          ticker=rd.ticker, decision_hash=rd.decision_hash,
                          signal_id=envelope["signal_id"],
                          verdict=gate.verdict)
        ok = self.notifier.send(self.notifier.signal_event(envelope))
        if not ok:
            self.audit.append("notifier_failed", "webhook dispatch failed; signal persisted",
                              ticker=rd.ticker, signal_id=envelope["signal_id"])
        return ProcessResult("emitted", envelope=envelope, reasons=gate.reasons)

    def _halted(self, reason: str, p: Path) -> ProcessResult:
        ep = read_episode(self.cfg.halt_latch_path) or {}
        self.audit.append("halted", reason, episode=ep.get("episode"), path=str(p))
        return ProcessResult("halted", reasons=(reason,))

    def _build_envelope(
        self,
        rd,
        contract,
        gate: GateResult,
        ref: RefData,
        seq: int,
        now: datetime,
    ) -> dict[str, Any]:
        band = self._cost_band(ref)
        return {
            "signal_id": f"sg-{rd.decision_hash[:8]}-{seq:03d}",
            "decision_hash": rd.decision_hash,
            "ticker": contract.symbol,
            "action": contract.action,
            "target_pct": contract.target_pct,
            "score": contract.score,
            "confidence": contract.confidence,
            "stop": contract.stop_price,
            "take_profit": contract.target_price,
            "expiry": contract.expiry,
            "ref": ref.as_dict(),
            "expected_cost_band_bps": band,
            "gates": {
                "verdict": gate.verdict,
                "downgrades": list(gate.downgrades),
                "blocked": list(gate.blocked),
                "reasons": list(gate.reasons),
                "target_notional_usd": gate.target_notional_usd,
                "approval_required": gate.approval_required,
            },
            "approval": {
                "state": "WAIT_FOR_APPROVAL" if gate.approval_required else "not_required"
            },
            "emitted_at": now.isoformat(timespec="seconds"),
            "config_hash": self.cfg.config_hash(),
            "commit": _commit(),
        }

    @staticmethod
    def _cost_band(ref: RefData) -> list[int]:
        """Half-spread baseline in bps, scaled by size (QSE caution)."""
        if ref.last and ref.spread_usd is not None:
            half = (ref.spread_usd / 2.0) / ref.last * 1e4
            return [max(1, int(half)), int(half) + 3]
        return [3, 8]


def _commit() -> str:
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - best-effort
        return ""
