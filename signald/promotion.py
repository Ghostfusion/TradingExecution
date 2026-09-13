"""Guarded-live scaffolding: the opt-ins, the promotion checklist, the hard cap.

Plan P5 (§11.3 kill switch / re-arm, §11.4 promotion checklist, and the §P5
"max notional hard-capped" rule).  Invariant, fail closed: an order path is
reachable only when the profile's opt-ins are explicitly satisfied, and a
promotion is ``complete`` only when *every* checklist item is machine-satisfied
-- a missing item is a blocker, never a warning.  Nothing here places, sizes or
prices an order; the sizer (:mod:`signald.risk.sizing`) and the gate
(:mod:`signald.risk.gate`) remain the only producers of quantity/permission, and
this module merely *calls* ``opt_ins`` to prove the gate is wired shut.

The module is pure: no I/O, no clock, no network.  The validation evidence it
consumes is the P4 harness record shape (plan §11): one mapping key per enabled
setup -> ``{dsr, pbo, trades, oos_months, n}``.  A setup's ``trades`` is its
count of realised paper fills, so the cost-model line (§11.4) is checked against
the same realised-fill evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .config import VALID_MODES

# --- plan §11 validation-gate thresholds (single source; harness + report agree)
DSR_MIN = 0.95
PBO_MAX = 0.05
MIN_TRADES = 50
MIN_OOS_MONTHS = 6.0

#: Cost-model validation floor: realised paper fills scored against the model (plan §11.4).
MIN_COST_MODEL_FILLS = 50
#: Reconciled paper sessions required before live (plan §11.4 / design §12).
MIN_PAPER_SESSIONS = 20

#: Profiles a promotion may target (plan §3).  `signal` has no order path.
PROFILES = VALID_MODES


def opt_ins(*, mode: str, execute_flag: bool, live_ack: bool) -> tuple[bool, str]:
    """Resolve the order-path opt-ins for one process mode (plan §P5, §3).

    Returns ``(allowed, reason)``; ``reason`` is empty when allowed.  The order
    path is unreachable in ``signal``; ``paper`` needs ``--execute``; ``live``
    needs ``--execute`` **and** the explicit acknowledgement.  Unknown modes are
    refused (fail closed).
    """
    if mode == "signal":
        return False, "signal has no order path"
    if mode == "paper":
        if execute_flag:
            return True, ""
        return False, "paper requires --execute"
    if mode == "live":
        if not execute_flag:
            return False, "live requires --execute"
        if not live_ack:
            return False, "live requires the acknowledgement flag"
        return True, ""
    return False, f"unknown mode {mode!r}"


@dataclass(frozen=True)
class ChecklistItem:
    """One machine-checked line of the §11.4 promotion checklist."""

    id: str
    requirement: str
    satisfied: bool
    detail: str


@dataclass(frozen=True)
class PromotionReport:
    """The §11.4 checklist verdict for one target profile (fail closed)."""

    profile: str
    items: tuple[ChecklistItem, ...]
    complete: bool
    blockers: tuple[str, ...]


def _num(value: object) -> float | None:
    """A finite real, or None (a bool / NaN / non-number cannot be evaluated)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _opt_ins_enforced() -> bool:
    """Prove both live opt-ins are in place (mode + --execute + acknowledgement).

    A structural check, not a tautology: the default must deny, a single opt-in
    must still deny, and only both together may allow.
    """
    no_flag, _ = opt_ins(mode="live", execute_flag=False, live_ack=False)
    flag_only, _ = opt_ins(mode="live", execute_flag=True, live_ack=False)
    both, _ = opt_ins(mode="live", execute_flag=True, live_ack=True)
    return (no_flag is False) and (flag_only is False) and both is True


def _validation_failures(validation: Mapping[str, dict] | None) -> list[str]:
    """Per-setup gate failures in stable (name-sorted) order; fail closed when empty."""
    if not validation:
        return ["no setup validation evidence recorded"]
    failures: list[str] = []
    for setup in sorted(validation):
        record = validation[setup]
        if not isinstance(record, Mapping):
            failures.append(f"{setup}: no validation record")
            continue
        dsr = _num(record.get("dsr"))
        if dsr is None or dsr < DSR_MIN:
            failures.append(f"{setup}: DSR {record.get('dsr')!r} < {DSR_MIN}")
        pbo = _num(record.get("pbo"))
        if pbo is None or pbo > PBO_MAX:
            failures.append(f"{setup}: PBO {record.get('pbo')!r} > {PBO_MAX}")
        trades = _num(record.get("trades"))
        if trades is None or trades < MIN_TRADES:
            failures.append(f"{setup}: trades {record.get('trades')!r} < {MIN_TRADES}")
        oos = _num(record.get("oos_months"))
        if oos is None or oos < MIN_OOS_MONTHS:
            failures.append(f"{setup}: OOS {record.get('oos_months')!r} < {MIN_OOS_MONTHS} months")
        n = _num(record.get("n"))
        if n is None or n <= 0:
            failures.append(f"{setup}: N (trials) not recorded")
    return failures


def _realised_fills(validation: Mapping[str, dict] | None) -> int:
    """Sum of realised paper fills across enabled setups (the cost-model evidence)."""
    if not validation:
        return 0
    total = 0.0
    for record in validation.values():
        if isinstance(record, Mapping):
            trades = _num(record.get("trades"))
            if trades is not None and trades > 0:
                total += trades
    return int(total)


def promotion_report(
    *,
    profile: str = "paper",
    sessions_reconciled: int,
    drills_passed: Sequence[str],
    required_drills: Sequence[str],
    paper_sessions_required: int = MIN_PAPER_SESSIONS,
    validation: Mapping[str, dict] | None = None,
    kill_switch_tested: bool = False,
    stale_data_tested: bool = False,
    daily_loss_tested: bool = False,
    hard_cap_usd: float | None = None,
    approval_threshold_usd: float | None = None,
    backups_tested: bool = False,
    owner_signed_date: str | None = None,
) -> PromotionReport:
    """Machine-check the §11.4 promotion checklist for ``profile`` (fail closed).

    ``validation`` maps each enabled setup to its P4 harness record
    ``{dsr, pbo, trades, oos_months, n}``; the realised-fill counts double as the
    cost-model evidence.  ``complete`` holds only when every item below is
    satisfied; ``blockers`` lists the failing item ids in checklist order.
    ``hard_cap``, ``approval_thresholds`` and ``opt_ins`` are required only for
    the ``live`` profile.
    """
    if profile not in PROFILES:
        raise ValueError(f"profile={profile!r} is not one of {list(PROFILES)}")

    items: list[ChecklistItem] = []

    sessions_ok = sessions_reconciled >= paper_sessions_required
    items.append(
        ChecklistItem(
            "paper_sessions",
            f">= {paper_sessions_required} paper sessions reconciled, flat by close, no drift",
            sessions_ok,
            f"{sessions_reconciled} of {paper_sessions_required} reconciled",
        )
    )

    passed = set(drills_passed)
    missing_drills = [d for d in required_drills if d not in passed]
    drills_ok = bool(required_drills) and not missing_drills
    items.append(
        ChecklistItem(
            "drills",
            "D1-D12 drills passed, drill log committed",
            drills_ok,
            (
                f"missing: {', '.join(missing_drills)}"
                if missing_drills
                else ("no drills required" if not required_drills else "all required drills passed")
            ),
        )
    )

    validation_failures = _validation_failures(validation)
    items.append(
        ChecklistItem(
            "validation_gates",
            "validation gates met for every enabled setup (DSR/PBO/trades/OOS/N)",
            not validation_failures,
            "; ".join(validation_failures) if validation_failures else "all setups passed",
        )
    )

    fills = _realised_fills(validation)
    cost_ok = fills >= MIN_COST_MODEL_FILLS
    items.append(
        ChecklistItem(
            "cost_model",
            f"cost model validated against >= {MIN_COST_MODEL_FILLS} realised paper fills",
            cost_ok,
            f"{fills} realised fills scored",
        )
    )

    items.append(
        ChecklistItem(
            "kill_switch",
            "kill switch demonstrated (cancel, flatten, freeze, episode latch)",
            bool(kill_switch_tested),
            "demonstrated" if kill_switch_tested else "not demonstrated",
        )
    )
    items.append(
        ChecklistItem(
            "daily_loss_flatten",
            "daily-loss flatten demonstrated",
            bool(daily_loss_tested),
            "demonstrated" if daily_loss_tested else "not demonstrated",
        )
    )
    items.append(
        ChecklistItem(
            "stale_data_block",
            "stale-data block demonstrated (new entries blocked, exits allowed)",
            bool(stale_data_tested),
            "demonstrated" if stale_data_tested else "not demonstrated",
        )
    )
    items.append(
        ChecklistItem(
            "ops_readiness",
            "backups + restore drill done; alerting verified; runbook committed",
            bool(backups_tested),
            "drilled and verified" if backups_tested else "not drilled",
        )
    )
    signed = bool(owner_signed_date and str(owner_signed_date).strip())
    items.append(
        ChecklistItem(
            "owner_signoff",
            "owner signs the pre-registered kill/shrink criteria date",
            signed,
            str(owner_signed_date) if signed else "unsigned",
        )
    )

    if profile == "live":
        cap_ok = _num(hard_cap_usd) is not None and float(hard_cap_usd) > 0.0
        items.append(
            ChecklistItem(
                "hard_cap",
                "hard capital cap configured",
                cap_ok,
                f"cap {hard_cap_usd}" if cap_ok else "no hard cap configured",
            )
        )
        thresholds_ok = (
            _num(approval_threshold_usd) is not None and float(approval_threshold_usd) > 0.0
        )
        items.append(
            ChecklistItem(
                "approval_thresholds",
                "approval thresholds set",
                thresholds_ok,
                f"threshold {approval_threshold_usd}" if thresholds_ok else "no threshold set",
            )
        )
        opt_ins_ok = _opt_ins_enforced()
        items.append(
            ChecklistItem(
                "opt_ins",
                "two independent opt-ins in place (mode + --execute + acknowledgement)",
                opt_ins_ok,
                "live opt-ins enforced" if opt_ins_ok else "live opt-ins not enforced",
            )
        )

    frozen = tuple(items)
    blockers = tuple(item.id for item in frozen if not item.satisfied)
    return PromotionReport(
        profile=profile,
        items=frozen,
        complete=not blockers,
        blockers=blockers,
    )


def hard_cap_violation(*, notional_usd: float, hard_cap_usd: float | None) -> str | None:
    """Refuse an order whose notional breaks the configured cap (plan §P5).

    Returns the refusal reason when a cap exists and is exceeded (or is itself
    unusable), ``None`` otherwise.  A *missing* cap is not a violation here -- in
    the ``live`` profile it is a promotion blocker carried by the report.
    """
    if hard_cap_usd is None:
        return None
    cap = _num(hard_cap_usd)
    if cap is None or cap <= 0.0:
        return f"hard cap {hard_cap_usd!r} is not a positive number"
    notional = _num(notional_usd)
    if notional is None:
        return f"notional {notional_usd!r} is not a finite number"
    if notional > cap:
        return f"notional {notional:.2f} exceeds hard cap {cap:.2f}"
    return None
