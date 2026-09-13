"""Lineage: trial registry, scorecard/comparator, and allocation manager.

Invariant (plan §A9, §10.3; design §7.3, §11.3-11.6): the trial count ``N`` is a
first-class output, and **no winner is declared** without the pre-registered
statistical bar - a short sample is reported as *feasibility, not superiority*.
Every statistic that cannot be computed is reported as unavailable with a
reason, never as a fabricated number, and the allocation manager only ever
proposes a bounded monthly tilt (never during a drawdown or a hot streak).

The statistics themselves live in ``signald.validation.stats`` (one
computation); this module only aligns trades into daily PnL series and calls
that implementation. The registry is append-only jsonl, written atomically via
``signald.stores._atomic_append``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import sha256_of
from .stores import AuditChain, _atomic_append

#: Design §11.1: an aggregate-Sharpe comparison needs ~3,500 sessions for t=2, so
#: every realistic window falls short of this bar and the honest claim is
#: feasibility. The bar is a constant so the wording is not a per-call opinion.
SESSIONS_BAR = 3500

FEASIBILITY_CLAIM = "feasibility, not superiority - no winner is declared"
ADEQUATE_CLAIM = (
    "the paired interval excludes zero; the pre-registered validation gates "
    "decide, not this report"
)
NO_DIFFERENCE_CLAIM = "no difference distinguishable from noise; no winner is declared"

#: The scorecard metrics, in report order. All are net of the rows' fees.
_METRICS: tuple[str, ...] = (
    "net_pnl_usd",
    "gross_pnl_usd",
    "fees_usd",
    "win_rate",
    "expectancy_usd",
    "expectancy_r",
    "avg_win_usd",
    "avg_loss_usd",
    "payoff_ratio",
    "profit_factor",
    "max_drawdown_usd",
    "mean_slippage_bps",
)

#: Diversification statistics (design §11.5); all ``float | None`` + reason.
_DIVERSIFICATION: tuple[str, ...] = (
    "correlation",
    "stress_correlation",
    "co_drawdown_incidence",
    "tail_joint_share",
    "tail_independence_share",
)


def _finite(value: Any) -> float | None:
    """A finite real number, or None. Booleans/NaN/strings are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _resolve_dsr() -> Callable[..., float]:
    """Resolve the DSR implementation lazily (lives in ``validation.stats``)."""
    from .validation.stats import deflated_sharpe

    return deflated_sharpe


def _resolve_paired() -> Callable[..., dict]:
    """Resolve the paired-bootstrap implementation lazily."""
    from .validation.stats import paired_comparison

    return paired_comparison


# --------------------------------------------------------------------------
# trial registry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Trial:
    """One recorded attempt; ``n_trials`` is the running count N at recording time."""

    trial_id: str
    variant: str
    started_at: str
    notes: str = ""
    metric: dict[str, float] = field(default_factory=dict)
    n_trials: int | None = None


def _clean_metric(metric: Mapping[str, float] | None) -> dict[str, float]:
    """Coerce a metric mapping to finite floats; a non-number fails closed."""
    if metric is None:
        return {}
    out: dict[str, float] = {}
    for key, value in metric.items():
        num = _finite(value)
        if num is None:
            raise ValueError(f"trial metric {key!r} is not a finite number: {value!r}")
        out[str(key)] = num
    return out


class TrialRegistry:
    """Append-only trial ledger (jsonl); ``count()`` is N (design §11.2)."""

    def __init__(
        self,
        path: str | Path,
        now: Callable[[], datetime],
        audit: AuditChain | None = None,
        dsrc: Callable[..., float] | None = None,
    ) -> None:
        self.path = Path(path)
        self._now = now
        self._audit = audit
        self._dsrc = dsrc

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for index, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # A silent skip would understate N, making the ledger unfalsifiable.
                raise ValueError(
                    f"trial registry row {index} is not valid JSON: {exc}"
                ) from exc
        return rows

    def trials(self) -> tuple[Trial, ...]:
        out: list[Trial] = []
        for row in self._rows():
            metric = row.get("metric") or {}
            out.append(
                Trial(
                    trial_id=str(row.get("trial_id", "")),
                    variant=str(row.get("variant", "")),
                    started_at=str(row.get("started_at", "")),
                    notes=str(row.get("notes", "")),
                    metric=_clean_metric(metric),
                    n_trials=row.get("n_trials"),
                )
            )
        return tuple(out)

    def count(self) -> int:
        """N: the number of trials recorded so far."""
        return len(self._rows())

    def register(
        self, variant: str, *, notes: str = "", metric: Mapping[str, float] | None = None
    ) -> Trial:
        """Record one trial. Every call is a new row with its own id (N grows)."""
        n = self.count() + 1
        trial = Trial(
            trial_id=f"T{n:04d}",
            variant=str(variant),
            started_at=self._now().isoformat(timespec="seconds"),
            notes=str(notes),
            metric=_clean_metric(metric),
            n_trials=n,
        )
        row = {
            "trial_id": trial.trial_id,
            "variant": trial.variant,
            "started_at": trial.started_at,
            "notes": trial.notes,
            "metric": trial.metric,
            "n_trials": trial.n_trials,
        }
        _atomic_append(self.path, json.dumps(row, sort_keys=True) + "\n")
        if self._audit is not None:
            self._audit.append(
                "trial_registered",
                f"trial {trial.trial_id} ({trial.variant})",
                trial_id=trial.trial_id,
                variant=trial.variant,
                n_trials=n,
            )
        return trial

    def deflated_sharpe(self, *, sharpe: float, observations: int) -> float:
        """DSR with N = this registry's count (the trial count is not an input)."""
        fn = self._dsrc if self._dsrc is not None else _resolve_dsr()
        return fn(sharpe=sharpe, n_trials=self.count(), observations=observations)


# --------------------------------------------------------------------------
# trade rows -> daily series
# --------------------------------------------------------------------------
def _day_of(row: Mapping[str, Any]) -> str | None:
    """The session date a trade's PnL realizes on (exit, else entry)."""
    for key in ("exit_ts", "entry_ts"):
        raw = row.get(key)
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return text[:10]
    return None


def _daily_pnl(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, float], str | None]:
    """Sum net PnL per session date; a single uncomputable row poisons the series."""
    days: dict[str, float] = {}
    bad_pnl = 0
    bad_day = 0
    for row in rows:
        qty = _finite(row.get("qty"))
        entry = _finite(row.get("entry_px"))
        exit_ = _finite(row.get("exit_px"))
        fees = _finite(row.get("fees_usd"))
        if qty is None or entry is None or exit_ is None or fees is None:
            bad_pnl += 1
            continue
        day = _day_of(row)
        if day is None:
            bad_day += 1
            continue
        net = qty * (exit_ - entry) - fees
        days[day] = days.get(day, 0.0) + net
    if bad_pnl:
        return {}, f"net PnL unavailable on {bad_pnl} trade(s)"
    if bad_day:
        return {}, f"session date unavailable on {bad_day} trade(s)"
    return days, None


def _aligned(
    days_a: Mapping[str, float], days_b: Mapping[str, float]
) -> tuple[list[str], list[float], list[float]]:
    """Align two sleeves' daily PnL on the union of sessions (missing day = 0.0)."""
    sessions = sorted(set(days_a) | set(days_b))
    series_a = [float(days_a.get(day, 0.0)) for day in sessions]
    series_b = [float(days_b.get(day, 0.0)) for day in sessions]
    return sessions, series_a, series_b


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Pearson correlation, or None when undefined (short/constant series)."""
    n = len(x)
    if n < 2 or len(y) != n:
        return None
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y, strict=True))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return num / math.sqrt(var_x * var_y)


def _max_drawdown(values: Sequence[float]) -> float:
    """Peak-to-trough of the cumulative path (starting peak 0)."""
    peak = 0.0
    cumulative = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


# --------------------------------------------------------------------------
# scorecard
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Scorecard:
    """One sleeve's metrics, net of fees; ``None`` metrics carry a reason."""

    sleeve: str
    trades: int
    metrics: dict[str, float | None]
    reasons: dict[str, str]
    sample_adequate: bool


def scorecard(
    trades: Sequence[Mapping[str, Any]], *, sleeve: str, minimum_trades: int = 30
) -> Scorecard:
    """Score one sleeve's trade rows (plan §10.1/§10.2, design §11.3).

    Every metric subtracts the row's ``fees_usd``; a metric that cannot be
    computed is ``None`` with a reason (never a fabricated number). Fewer than
    ``minimum_trades`` rows fails the sample bar (insufficient evidence).
    """
    rows = list(trades)
    n = len(rows)
    metrics: dict[str, float | None] = dict.fromkeys(_METRICS)
    reasons: dict[str, str] = {}
    if n == 0:
        return Scorecard(
            sleeve=sleeve,
            trades=0,
            metrics=metrics,
            reasons=dict.fromkeys(_METRICS, "no trades"),
            sample_adequate=False,
        )

    grosses: list[float] = []
    nets: list[float] = []
    fees_vals: list[float] = []
    slips: list[float] = []
    rs: list[float] = []
    bad_core = 0
    bad_fees = 0
    for row in rows:
        qty = _finite(row.get("qty"))
        entry = _finite(row.get("entry_px"))
        exit_ = _finite(row.get("exit_px"))
        fees = _finite(row.get("fees_usd"))
        if qty is None or entry is None or exit_ is None:
            bad_core += 1
        else:
            gross = qty * (exit_ - entry)
            grosses.append(gross)
            if fees is None:
                bad_fees += 1
            else:
                fees_vals.append(fees)
                nets.append(gross - fees)
        slip = _finite(row.get("slippage_bps"))
        if slip is not None:
            slips.append(slip)
        r_mult = _finite(row.get("r_multiple"))
        if r_mult is not None:
            rs.append(r_mult)

    core_ok = bad_core == 0
    fees_ok = core_ok and bad_fees == 0
    core_reason = f"qty/entry_px/exit_px unavailable on {bad_core} trade(s)"
    fees_reason = f"fees_usd unavailable on {bad_fees} trade(s)"
    pnl_reason = core_reason if bad_core else fees_reason

    metrics["gross_pnl_usd"] = float(sum(grosses)) if core_ok else None
    if fees_ok:
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x < 0]
        metrics["fees_usd"] = float(sum(fees_vals))
        metrics["net_pnl_usd"] = float(sum(nets))
        metrics["win_rate"] = sum(1 for x in nets if x > 0) / n
        metrics["expectancy_usd"] = sum(nets) / n
        metrics["max_drawdown_usd"] = _max_drawdown(nets)
        if wins:
            metrics["avg_win_usd"] = sum(wins) / len(wins)
        if losses:
            # Reported as a magnitude so "Avg Win / Avg Loss" reads as a ratio.
            metrics["avg_loss_usd"] = abs(sum(losses) / len(losses))
        if wins and losses:
            metrics["payoff_ratio"] = metrics["avg_win_usd"] / metrics["avg_loss_usd"]
            metrics["profit_factor"] = sum(wins) / abs(sum(losses))
    metrics["expectancy_r"] = (sum(rs) / n) if len(rs) == n else None
    metrics["mean_slippage_bps"] = (sum(slips) / len(slips)) if slips else None

    for key in _METRICS:
        if metrics[key] is not None:
            continue
        if key == "gross_pnl_usd":
            reasons[key] = core_reason
        elif not fees_ok:
            reasons[key] = pnl_reason
        elif key == "avg_win_usd":
            reasons[key] = "no winning trades"
        elif key == "avg_loss_usd":
            reasons[key] = "no losing trades"
        elif key == "payoff_ratio":
            reasons[key] = "requires both winning and losing trades"
        elif key == "profit_factor":
            reasons[key] = "no losing trades (undefined)"
        elif key == "expectancy_r":
            reasons[key] = f"r_multiple unavailable on {n - len(rs)} trade(s)"
        elif key == "mean_slippage_bps":
            reasons[key] = "slippage_bps unavailable on all trades"
        else:
            reasons[key] = "unavailable"

    return Scorecard(
        sleeve=sleeve,
        trades=n,
        metrics=metrics,
        reasons=reasons,
        sample_adequate=n >= minimum_trades,
    )


def compare(
    trades_a: Sequence[Mapping[str, Any]],
    trades_b: Sequence[Mapping[str, Any]],
    *,
    sleeve_a: str,
    sleeve_b: str,
    minimum_trades: int = 30,
    paired: Callable[..., dict] | None = None,
) -> dict:
    """Paired sleeve comparison (plan §10.3, design §11.4).

    Both sleeves' trade rows are collapsed to daily PnL on the union of
    sessions (a sleeve with no trade that day contributes 0.0), then
    ``validation.stats.paired_comparison`` returns the stationary-bootstrap
    interval on the daily difference. The ``claim`` never declares a winner:
    below the pre-registered bar it is the feasibility sentence.
    """
    card_a = scorecard(trades_a, sleeve=sleeve_a, minimum_trades=minimum_trades)
    card_b = scorecard(trades_b, sleeve=sleeve_b, minimum_trades=minimum_trades)
    days_a, why_a = _daily_pnl(trades_a)
    days_b, why_b = _daily_pnl(trades_b)
    sessions, series_a, series_b = _aligned(days_a, days_b)
    difference: dict[str, Any] = {
        "sessions": len(sessions),
        "mean_diff": None,
        "ci_low": None,
        "ci_high": None,
        "share_positive": None,
        "excludes_zero": None,
        "n": None,
        "reason": None,
    }
    if why_a or why_b:
        difference["reason"] = why_a or why_b
    elif len(sessions) < 2:
        difference["reason"] = "fewer than two aligned sessions"
    elif not days_a or not days_b:
        difference["reason"] = "one sleeve has no daily PnL to pair"
    else:
        fn = paired if paired is not None else _resolve_paired()
        result = fn(series_a, series_b)
        difference["mean_diff"] = result.get("mean_diff")
        difference["ci_low"] = result.get("ci_low")
        difference["ci_high"] = result.get("ci_high")
        difference["share_positive"] = result.get("share_positive")
        difference["excludes_zero"] = result.get("excludes_zero")
        difference["n"] = result.get("n")

    adequate = card_a.sample_adequate and card_b.sample_adequate
    short = len(sessions) < SESSIONS_BAR or not adequate
    if short:
        claim = FEASIBILITY_CLAIM
    elif difference.get("excludes_zero"):
        claim = ADEQUATE_CLAIM
    else:
        claim = NO_DIFFERENCE_CLAIM
    return {"a": card_a, "b": card_b, "difference": difference, "claim": claim}


# --------------------------------------------------------------------------
# diversification evidence
# --------------------------------------------------------------------------
def diversification(
    trades_a: Sequence[Mapping[str, Any]], trades_b: Sequence[Mapping[str, Any]]
) -> dict:
    """Diversification evidence before any capital tilt (design §11.5).

    Reports correlation of daily sleeve PnL, stress correlation on the worst
    decile of days, co-drawdown incidence, and tail-dependence joint
    exceedance. Every number is ``None`` with a reason when uncomputable.
    """
    days_a, why_a = _daily_pnl(trades_a)
    days_b, why_b = _daily_pnl(trades_b)
    sessions, series_a, series_b = _aligned(days_a, days_b)
    n = len(sessions)
    out: dict[str, Any] = {"days": n}
    out.update(dict.fromkeys(_DIVERSIFICATION))
    reasons: dict[str, str] = {}
    out["reasons"] = reasons

    if why_a or why_b:
        for key in _DIVERSIFICATION:
            reasons[key] = why_a or why_b
        return out
    if n == 0:
        for key in _DIVERSIFICATION:
            reasons[key] = "no sessions to align"
        return out

    out["correlation"] = _pearson(series_a, series_b)
    if out["correlation"] is None:
        reasons["correlation"] = (
            "fewer than two sessions" if n < 2 else "daily PnL has no variance"
        )

    decile = n // 10
    if decile >= 2:
        worst = sorted(range(n), key=lambda i: series_a[i] + series_b[i])[:decile]
        out["stress_correlation"] = _pearson(
            [series_a[i] for i in worst], [series_b[i] for i in worst]
        )
        if out["stress_correlation"] is None:
            reasons["stress_correlation"] = "worst-decile PnL has no variance"
    else:
        reasons["stress_correlation"] = (
            f"worst decile of {n} session(s) has fewer than two points"
        )

    out["co_drawdown_incidence"] = _co_drawdown(series_a, series_b)

    if n >= 20:
        tail = max(1, n // 20)
        worst_a = set(sorted(range(n), key=lambda i: series_a[i])[:tail])
        worst_b = set(sorted(range(n), key=lambda i: series_b[i])[:tail])
        out["tail_joint_share"] = len(worst_a & worst_b) / n
        out["tail_independence_share"] = (tail / n) ** 2
    else:
        reasons["tail_joint_share"] = f"fewer than 20 sessions ({n}) for a 5% tail"
        reasons["tail_independence_share"] = reasons["tail_joint_share"]
    return out


def _co_drawdown(series_a: Sequence[float], series_b: Sequence[float]) -> float | None:
    """Fraction of sessions on which both sleeves were below their running peak."""
    n = len(series_a)
    if n == 0:
        return None
    peak_a = cum_a = peak_b = cum_b = 0.0
    both = 0
    for x, y in zip(series_a, series_b, strict=True):
        cum_a += x
        peak_a = max(peak_a, cum_a)
        cum_b += y
        peak_b = max(peak_b, cum_b)
        if cum_a < peak_a and cum_b < peak_b:
            both += 1
    return both / n


# --------------------------------------------------------------------------
# allocation manager
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AllocationDecision:
    """One sleeve's monthly-review proposal; ``frozen`` means: nothing moves."""

    sleeve: str
    current_pct: float
    proposed_pct: float
    capped_by: str | None
    reason: str
    frozen: bool


def _hard_ceiling(sleeve: str, config: Any, ceiling_pct: float) -> float:
    """The effective ceiling: the tighter of ``ceiling_pct`` and the sleeve cap."""
    field_name = {
        "swing": "sleeve_swing_capital_pct",
        "intraday": "sleeve_intraday_capital_pct",
    }.get(sleeve)
    hard = _finite(getattr(config, field_name, None)) if field_name else None
    return min(ceiling_pct, hard) if hard is not None else ceiling_pct


def propose_allocation(
    *,
    current: Mapping[str, float],
    evidence: Mapping[str, dict],
    config: Any,
    frozen: bool = False,
    floor_pct: float = 0.15,
    ceiling_pct: float = 0.85,
    band_pct: float = 0.10,
) -> tuple[AllocationDecision, ...]:
    """Monthly reallocation proposal (design §7.3). One decision per current sleeve.

    ``evidence[sleeve]`` fields: ``clears_noise_bar`` (bool, required to act),
    ``tilt_pct`` (signed fraction, e.g. 0.05 = +5pp), ``in_drawdown`` (bool),
    ``hot_streak`` (bool). Without evidence clearing the pre-registered noise
    bar the proposal is the current allocation unchanged. A tilt is bounded by
    ``band_pct`` per sleeve and clamped to ``[floor_pct, ceiling_pct]`` (and to
    the configured sleeve capital ceiling). Cadence is the caller's decision.
    """
    decisions: list[AllocationDecision] = []
    for sleeve in sorted(current):
        cur = float(current[sleeve])
        ev = evidence.get(sleeve) or {}
        if frozen:
            decisions.append(
                AllocationDecision(sleeve, cur, cur, None, "allocation frozen", True)
            )
            continue
        if not ev.get("clears_noise_bar"):
            decisions.append(
                AllocationDecision(
                    sleeve,
                    cur,
                    cur,
                    None,
                    "no evidence clearing the pre-registered noise bar",
                    False,
                )
            )
            continue
        if ev.get("in_drawdown"):
            decisions.append(
                AllocationDecision(
                    sleeve, cur, cur, None, "active drawdown - reallocation forbidden", False
                )
            )
            continue
        if ev.get("hot_streak"):
            decisions.append(
                AllocationDecision(
                    sleeve, cur, cur, None, "hot-streak reallocation refused", False
                )
            )
            continue
        tilt = _finite(ev.get("tilt_pct"))
        if tilt is None or tilt == 0.0:
            decisions.append(
                AllocationDecision(
                    sleeve,
                    cur,
                    cur,
                    None,
                    "evidence clears the noise bar but requests no tilt",
                    False,
                )
            )
            continue
        target = cur + tilt
        capped_by: str | None = None
        if abs(tilt) > band_pct:
            target = cur + math.copysign(band_pct, tilt)
            capped_by = "band"
        cap = _hard_ceiling(sleeve, config, ceiling_pct)
        if target < floor_pct:
            target = floor_pct
            capped_by = "floor"
        elif target > cap:
            target = cap
            capped_by = "ceiling"
        decisions.append(
            AllocationDecision(sleeve, cur, target, capped_by, "monthly review tilt", False)
        )
    return tuple(decisions)


# --------------------------------------------------------------------------
# committed scorecard artifact
# --------------------------------------------------------------------------
def _scorecard_dict(card: Scorecard) -> dict[str, Any]:
    return {
        "sleeve": card.sleeve,
        "trades": card.trades,
        "sample_adequate": card.sample_adequate,
        "metrics": dict(card.metrics),
        "reasons": dict(card.reasons),
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.6g}"


def _scorecard_markdown(payload: Mapping[str, Any]) -> str:
    lines = ["# Scorecard", ""]
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Trials (N): {payload['trials']}")
    lines.append(f"Claim limit: {payload['claim_limit']}")
    lines.append("")
    for name, card in payload["scorecards"].items():
        adequacy = "" if card["sample_adequate"] else " (insufficient evidence)"
        lines.append(f"## {name}")
        lines.append(f"- trades: {card['trades']}{adequacy}")
        for key in sorted(card["metrics"]):
            value = card["metrics"][key]
            if value is None:
                lines.append(f"- {key}: n/a ({card['reasons'].get(key, 'unavailable')})")
            else:
                lines.append(f"- {key}: {_fmt(value)}")
        lines.append("")
    comparison = payload.get("comparison")
    if comparison:
        diff = comparison["difference"]
        lines.append("## comparison")
        lines.append(f"- sessions: {diff.get('sessions')}")
        lines.append(f"- mean_diff: {_fmt(diff.get('mean_diff'))}")
        lines.append(f"- ci: [{_fmt(diff.get('ci_low'))}, {_fmt(diff.get('ci_high'))}]")
        lines.append(f"- claim: {comparison['claim']}")
        if diff.get("reason"):
            lines.append(f"- reason: {diff['reason']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_scorecard(
    path: str | Path,
    *,
    scorecards: Mapping[str, Scorecard],
    comparison: Mapping | None,
    trials: int,
    generated_at: datetime,
) -> Path:
    """Commit the scorecard as ``<path>.json`` + ``<path>.md`` (plan §10.3).

    Both artifacts name N and the claim limit; a short sample states the
    feasibility limit. Returns the markdown path (the human artifact).
    """
    short = comparison is None or any(not card.sample_adequate for card in scorecards.values())
    payload: dict[str, Any] = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "trials": int(trials),
        "claim_limit": FEASIBILITY_CLAIM if short else ADEQUATE_CLAIM,
        "scorecards": {name: _scorecard_dict(card) for name, card in sorted(scorecards.items())},
        "comparison": None,
    }
    if comparison is not None:
        payload["comparison"] = {
            "a": _scorecard_dict(comparison["a"]),
            "b": _scorecard_dict(comparison["b"]),
            "difference": dict(comparison["difference"]),
            "claim": comparison["claim"],
        }
    payload["artifact_sha256"] = sha256_of(payload)

    base = str(path)
    json_path = Path(base + ".json")
    md_path = Path(base + ".md")
    _atomic_write(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _atomic_write(md_path, _scorecard_markdown(payload))
    return md_path

# --------------------------------------------------------------------------
# Pre-registered kill / shrink criteria (design §7.6, plan §P6)
# --------------------------------------------------------------------------
KILL = "kill"
SHRINK = "shrink"
KEEP = "keep"
#: The order the criteria are evaluated in: the first one that matches decides.
REVIEW_CRITERIA = (
    "negative_expectation",
    "cvar_grows_without_return",
    "weak_risk_adjusted_contribution",
    "insufficient_evidence",
    "keep",
)
#: A sleeve must have at least this many trades before a verdict may act on it.
MIN_TRADES_FOR_VERDICT = 100


@dataclass(frozen=True)
class ReviewDecision:
    """The mechanical outcome of the pre-registered criteria - never a judgement call."""

    sleeve: str
    verdict: str            # kill | shrink | keep
    reason_code: str        # one of REVIEW_CRITERIA
    detail: str
    target_pct: float | None = None

    @property
    def acts(self) -> bool:
        return self.verdict in {KILL, SHRINK}


def kill_shrink_decision(
    *,
    sleeve: str,
    trades: int,
    expectancy_usd: float | None,
    current_pct: float,
    sample_adequate: bool,
    bootstrap: Mapping[str, float] | None = None,
    predicted_expectancy_usd: float | None = None,
    cvar_share: float | None = None,
    return_share: float | None = None,
    shrink_pct: float = 0.10,
) -> ReviewDecision:
    """Evaluate design §7.6 in order; the decision is computed from the journal.

    Every input is evidence, never opinion: an unavailable input cannot *cause* a
    kill (it falls through), and a kill requires an adequate sample - stopping a
    live sleeve on noise is the failure mode the pre-registration exists to
    prevent. The verdict is executed mechanically; sunk cost is not an input.
    """
    enough = trades >= MIN_TRADES_FOR_VERDICT and sample_adequate
    ci_excludes_prediction = False
    if bootstrap and predicted_expectancy_usd is not None:
        low, high = bootstrap.get("ci_low"), bootstrap.get("ci_high")
        if low is not None and high is not None:
            ci_excludes_prediction = not (float(low) <= predicted_expectancy_usd <= float(high))

    if (
        enough
        and expectancy_usd is not None
        and expectancy_usd <= 0
        and (predicted_expectancy_usd is None or ci_excludes_prediction)
    ):
        suffix = (
            " with the interval excluding the backtest prediction"
            if ci_excludes_prediction
            else ""
        )
        return ReviewDecision(
            sleeve,
            KILL,
            "negative_expectation",
            f"net expectancy {expectancy_usd:,.2f} over {trades} trades{suffix}",
        )

    if cvar_share is not None and return_share is not None and cvar_share > return_share:
        return ReviewDecision(
            sleeve,
            KILL,
            "cvar_grows_without_return",
            f"CVaR share {cvar_share:.2f} exceeds return share {return_share:.2f}",
        )

    if enough and expectancy_usd is not None and expectancy_usd > 0:
        if bootstrap and not bool(bootstrap.get("excludes_zero")):
            return ReviewDecision(
                sleeve,
                SHRINK,
                "weak_risk_adjusted_contribution",
                "positive but the paired interval does not clear zero",
                target_pct=shrink_pct,
            )
        return ReviewDecision(
            sleeve,
            KEEP,
            "keep",
            f"expectancy {expectancy_usd:,.2f} over {trades} trades with an interval clear of zero",
            target_pct=current_pct,
        )

    return ReviewDecision(
        sleeve,
        KEEP,
        "insufficient_evidence",
        f"{trades} trades (need {MIN_TRADES_FOR_VERDICT} and an adequate sample) - no action",
        target_pct=current_pct,
    )


def write_review_report(
    path: str | Path,
    *,
    decisions: Sequence[ReviewDecision],
    generated_at: datetime,
    trials: int,
) -> Path:
    """Write the committed review note: the verdicts, N, and the claim limit."""
    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "trials": int(trials),
        "claim_limit": FEASIBILITY_CLAIM,
        "decisions": [
            {
                "sleeve": d.sleeve,
                "verdict": d.verdict,
                "reason_code": d.reason_code,
                "detail": d.detail,
                "target_pct": d.target_pct,
            }
            for d in decisions
        ],
    }
    payload["artifact_sha256"] = sha256_of(payload)
    base = str(path)
    _atomic_write(Path(base + ".json"), json.dumps(payload, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Parallel-review decision",
        "",
        f"Generated: {payload['generated_at']}",
        f"Trials (N): {payload['trials']}",
        f"Claim limit: {payload['claim_limit']}",
        "",
    ]
    for d in decisions:
        target = "" if d.target_pct is None else f" -> {d.target_pct:.0%}"
        lines.append(f"- {d.sleeve}: **{d.verdict}** ({d.reason_code}){target} - {d.detail}")
    lines.append("")
    _atomic_write(Path(base + ".md"), "\n".join(lines))
    return Path(base + ".md")
