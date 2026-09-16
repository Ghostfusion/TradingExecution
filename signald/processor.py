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
from .inbox import Inbox
from .kill_switch import ensure_episode, is_halted, read_episode
from .mandate import Mandate, MandateError, load_mandate
from .notifier import Notifier
from .schema import ContractError, build_signal_contract, parse_research_decision
from .sleeves.router import RouteError, route_research
from .stores import AuditChain, CandidateStore, Journal, SignalStore

#: Ratings (case-insensitive) that make a not-held name worth offering to the
#: operator. Deliberately narrow: "buy"/"overweight" only, so the queue is the
#: handful of names research actually likes rather than everything it mentions.
PROMOTABLE_RATINGS: frozenset[str] = frozenset({"buy", "overweight"})


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
        inbox: Inbox | None = None,
        candidates: CandidateStore | None = None,
    ) -> None:
        self.cfg = config
        self.mandate = mandate
        self.store = store
        self.journal = journal
        self.audit = audit
        self.ref = reference
        self.notifier = notifier
        self.inbox = inbox
        #: Candidates default to the data dir so every construction path (CLI,
        #: tests, control surfaces) gets the queue without extra plumbing.
        self.candidates = candidates or CandidateStore(
            Path(config.data_dir) / "mandate_candidates.jsonl"
        )
        self._mandate_hash = mandate.hash
        #: (mtime_ns, size) of the mandate file as last loaded; None forces the
        #: first refresh to prove the file still matches the loaded mandate.
        self._mandate_stamp: tuple[int, int] | None = None
        self._reload_failure: str | None = None

    def process(self, path: str | Path) -> ProcessResult:
        p = Path(path)
        now = self.cfg.now()

        if self.mandate.is_expired(now):
            return self._halted("mandate expired", p)

        if is_halted(self.cfg.kill_switch_path):
            ep = ensure_episode(self.cfg.halt_latch_path, now)
            return self._halted(f"kill switch {ep['episode']}", p)

        # 0. boundary: admit + dedupe (own-before-write, effectively once)
        admission = self.inbox.admit(p) if self.inbox is not None else None
        if admission is not None and admission.kind != "accepted":
            if admission.kind == "deduped":
                return ProcessResult("skipped_duplicate", reasons=(admission.detail,))
            return ProcessResult(
                "dead_lettered",
                reasons=(f"{admission.reason_code}: {admission.detail}",),
            )

        # 1. load + validate
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            rd = parse_research_decision(raw)
        except (json.JSONDecodeError, OSError, ContractError) as exc:
            self.audit.append("rejected_invalid", f"{p.name}: {exc}", path=str(p))
            self._notify_error("invalid_decision", f"{p.name}: {exc}")
            return ProcessResult("invalid", reasons=(str(exc),))

        # 2. prechecks: future date + ingest window
        if rd.effective_date > now.date():
            self.audit.append("rejected", "effective_date in the future (no lookahead)",
                              ticker=rd.ticker, path=str(p))
            return ProcessResult("blocked", reasons=("effective_date in the future",))

        # 3. idempotency. The skip is silent on purpose: the poll re-discovers
        # every artifact still sitting in the watch tree, and the audit ledger
        # records decisions, not poll cycles (2026-09-15: one artifact in
        # reports/ added a skipped_duplicate row every 10 s, ~8.6k rows/day of
        # pure noise). The decision itself was recorded when it was handled.
        if self.journal.is_processed(rd.decision_hash, mandate_hash=self._mandate_hash):
            return ProcessResult("skipped_duplicate")

        # 4. reference data (fail-closed when unavailable)
        try:
            ref = self.ref.snapshot(rd.ticker)
        except ReferenceUnavailable as exc:
            if self.cfg.ref_required:
                self.audit.append("rejected", f"reference unavailable: {exc}",
                                  ticker=rd.ticker, path=str(p))
                self._notify_error("reference_unavailable", f"{rd.ticker}: {exc}")
                return ProcessResult("blocked", reasons=(f"reference unavailable: {exc}",))
            ref = RefData()
            self.audit.append("warn", f"reference unavailable (ref_required=false): {exc}",
                              ticker=rd.ticker)

        # 5. normalize -> contract, then route (the router owns the sleeve)
        contract = build_signal_contract(rd, self.mandate.expires.isoformat(), now, ref.equity)
        try:
            route = route_research(rd, self.cfg)
        except RouteError as exc:
            self.audit.append("rejected", f"sleeve routing refused: {exc}", ticker=rd.ticker)
            self._notify_error("routing_refused", f"{rd.ticker}: {exc}")
            if self.inbox is not None:
                self.inbox.quarantine(
                    {"ticker": rd.ticker, "decision_hash": rd.decision_hash},
                    "sleeve_routing",
                    str(exc),
                )
            return ProcessResult("blocked", reasons=(f"sleeve routing refused: {exc}",))

        # 6. gates
        journal_state = {
            "signals_today": self.journal.signals_today_count(now),
            "last_signal": self.journal.last_signal(),
            "cooldown_hours": self.cfg.cooldown_hours,
            "ingest_window_hours": self.cfg.ingest_window_hours,
            "approval_threshold_factor": self.cfg.approval_threshold_factor,
        }
        gate: GateResult = evaluate(
            rd, contract, self.mandate, ref, journal_state, now, self.cfg
        )

        if gate.verdict == "BLOCK":
            # A refusal is a durable verdict on this artifact (the mandate does
            # not list the symbol, the book is out of room), so record it as
            # processed like an emission. Without this the reports poll re-fires
            # the same refusal every cycle: measured 2026-09-15 an ineligible
            # artifact produced a rejected row + a Discord error card every 10 s
            # (4 in 44 s). The row carries the mandate hash, so a deliberate
            # ``signald mandate-add`` re-opens the artifact exactly once.
            self.journal.mark_processed(rd.decision_hash, rd.ticker, str(p),
                                        outcome="blocked", mandate_hash=self._mandate_hash)
            self.audit.append(
                "rejected",
                "; ".join(gate.blocked),
                ticker=rd.ticker, decision_hash=rd.decision_hash,
                gate_reasons=list(gate.reasons),
            )
            if not self._offer_candidate(rd, contract, ref, gate):
                self._notify_error("signal_blocked", f"{rd.ticker}: {'; '.join(gate.blocked)}")
            if self.inbox is not None and admission is not None:
                # valid artifact, not permissible: quarantine, never discard silently
                self.inbox.quarantine(
                    {
                        "ticker": rd.ticker,
                        "action": contract.action,
                        "decision_hash": rd.decision_hash,
                        "sleeve": route.sleeve,
                        "binding_gate": gate.binding_gate,
                        "reasons": list(gate.blocked),
                    },
                    gate.binding_gate or "gate_block",
                    "; ".join(gate.blocked),
                    key=admission.key,
                )
            return ProcessResult("blocked", reasons=gate.blocked)

        # 7. envelope
        seq = len(self.store.read_all()) + 1
        envelope = self._build_envelope(rd, contract, gate, ref, seq, now, route.sleeve)

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
        if self.inbox is not None and admission is not None:
            self.inbox.commit(admission.key, signal_id=envelope["signal_id"])
        return ProcessResult("emitted", envelope=envelope, reasons=gate.reasons)

    def _offer_candidate(self, rd, contract, ref: RefData, gate: GateResult) -> bool:
        """Queue a strongly-rated, not-held name the mandate bars; True when queued.

        Option A (2026-09-15): the executor never widens its own mandate. This
        records the name for the operator and pages the exact promotion command
        instead of a bare refusal. It fires only when the *symbol* check is what
        blocked the decision, the rating is buy/overweight, and the account
        provably does not hold the name - unknown holdings are not proof, so they
        keep the plain refusal (fail closed).
        """
        ticker = str(rd.ticker).upper()
        if ticker in self.mandate.allowed:
            return False
        if gate.binding_gate != "mandate" or "not in mandate" not in " ".join(gate.blocked):
            return False
        rating = str(rd.rating or "").strip().lower()
        if rating not in PROMOTABLE_RATINGS:
            return False
        if ref.held_symbols is None or ticker in ref.held_symbols:
            return False
        if self.candidates.has(ticker, rd.decision_hash):
            return False
        producer = rd.extra.get("producer")
        run_id = producer.get("run_id") if isinstance(producer, dict) else None
        run_id = run_id or rd.extra.get("run_id")
        run_id = None if run_id is None else str(run_id)
        at = self.cfg.now().isoformat(timespec="seconds")
        self.candidates.record(
            ticker=ticker, rating=str(rd.rating), action=contract.action,
            decision_hash=rd.decision_hash, target_pct=contract.target_pct,
            stop=contract.stop_price, run_id=run_id, at=at,
        )
        self.audit.append("mandate_candidate",
                          f"{ticker} {contract.action} ({rating}) is outside the mandate",
                          ticker=ticker, decision_hash=rd.decision_hash,
                          rating=str(rd.rating), action=contract.action)
        if self.notifier.enabled:
            self.notifier.send(self.notifier.mandate_candidate_event(
                ticker=ticker, action=contract.action, rating=str(rd.rating),
                decision_hash=rd.decision_hash, target_pct=contract.target_pct,
                stop=contract.stop_price, run_id=run_id,
            ))
        return True

    def refresh_mandate(self) -> str:
        """Reload the mandate file when it changes: ``unchanged|reloaded|invalid``.

        The mandate is loaded once at startup, so without this an operator's
        ``signald mandate-add`` would only take effect after a daemon restart
        (the poll loop reuses one ``Mandate`` object). Fail closed: a file that
        no longer parses keeps the loaded mandate, and that failure is audited
        and paged once per distinct reason rather than on every poll.
        """
        path = Path(self.cfg.mandate_path)
        try:
            st = path.stat()
        except OSError as exc:
            return self._reload_failed(f"{path}: {exc}")
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._mandate_stamp:
            return "unchanged"
        try:
            fresh = load_mandate(path)
        except MandateError as exc:
            return self._reload_failed(str(exc))
        if fresh.hash == self._mandate_hash:
            self._mandate_stamp = stamp
            return "unchanged"
        previous, self.mandate = self._mandate_hash, fresh
        self._mandate_hash = fresh.hash
        self._mandate_stamp = stamp
        self._reload_failure = None
        self.audit.append("mandate_reloaded", f"mandate {fresh.id} re-signed",
                          old_hash=previous, new_hash=fresh.hash,
                          allowed=sorted(fresh.allowed))
        return "reloaded"

    def _reload_failed(self, detail: str) -> str:
        if detail != self._reload_failure:
            self._reload_failure = detail
            self.audit.append("mandate_reload_failed", detail,
                              path=str(self.cfg.mandate_path))
            self._notify_error("mandate_reload_failed", detail)
        return "invalid"

    def _notify_error(self, source: str, detail: str) -> None:
        if self.notifier.enabled:
            self.notifier.send(self.notifier.error_event(source, detail))

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
        sleeve: str,
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
            # --- v2 (additive; plan §2.3) --------------------------------
            "sleeve": sleeve,
            "opportunity_score": contract.opportunity_score,
            "trade_permission": gate.trade_permission,
            "binding_gate": gate.binding_gate,
            "permission_reason_code": gate.permission_reason_code,
            "permission_reason": "; ".join(gate.blocked or gate.downgrades or gate.reasons),
            "idempotency_key": contract.idempotency_key,
            "valid_until": contract.valid_until,
            "stop_kind": contract.stop_kind or "native",
            "schema_version": "1.1.0",
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
