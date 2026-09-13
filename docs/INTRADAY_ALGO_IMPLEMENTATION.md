# Two-Sleeve Algo Intraday Execution — Implementation Plan

Status: **Draft v1 — plan only. This document writes no code and enables nothing.**
Owner: single operator. Date: 2026-09-12. Interpreter: `py -3.12`. Linter: ruff (E/W/F/I/B/UP/C4/SIM, line 100).

Companion and authority for *what* and *why*: [`INTRADAY_ALGO_DESIGN.md`](INTRADAY_ALGO_DESIGN.md).
Authority for *when*: the phase gates in §8. Authority for *how it is verified*: §9.

---

## 0. Where this build starts

### 0.1 What already exists (`signald`, Phase A — signal mode, no order path)

| Module | What it does today | Reused by this plan |
|---|---|---|
| `signald/watch.py` | recursive discovery of `research_decision.json`, newest-per-symbol | yes (becomes one of two inbox producers) |
| `signald/schema.py` | `ResearchDecision` validation + hash-pinning; `SignalContract`; `ContractError` | extended (§2) |
| `signald/mandate.py` | hash-pinned signed mandate, expiry, re-sign + archive | extended (sleeve ceilings, approval mode) |
| `signald/gates.py` | fail-closed mandate gates returning `GateResult(verdict, blocked, downgrades, reasons, target_notional_usd, approval_required)` | folded into the house gate (§4, C7) |
| `signald/alpaca_ref.py` | reference reads only: quote/bar, account, clock, assets, positions; injectable seam; `NOT_IMPLEMENTED` guard on submission | becomes the read half of the broker adapter |
| `signald/stores.py` | `signals.jsonl` + `latest.json`, idempotency journal, SHA-256-chained audit ledger + `verify` | extended with sleeves, orders, fills, inbox, proposals, trial registry |
| `signald/notifier.py` | journal-first webhook events (signal/error/kill_switch) | extended alert set (§9.4) |
| `signald/kill_switch.py` | filesystem sentinel + persisted HALT episode latch | unchanged, plus house-wide flatten |
| `signald/daemon.py` | PID lockfile, heartbeat | unchanged |
| `signald/cli.py` | `run · verify · status · sample · approve · init-mandate · notify-test · watchdog`; dry-run default; `--execute` opt-in | extended (`simulate`, `propose`, `flatten`, `scorecard`) |
| `signald/watchdog.py` | heartbeat freshness + `heartbeat_loss` page | unchanged |

Today's envelope already carries `signal_id`, `decision_hash`, `ticker`, `action`, `target_pct`, `score`,
`confidence`, `stop`, `take_profit`, `expiry`, `ref`, `expected_cost_band_bps`, `gates{verdict, downgrades,
blocked, reasons, target_notional_usd, approval_required}`, `approval.state`, `emitted_at`, `config_hash`,
`commit`.

### 0.2 What this plan adds

| # | Addition | Design ref |
|---|---|---|
| A1 | Inbox + dedupe table (effectively-once ingest) and versioned envelopes | §2.2, §12.4 |
| A2 | Sleeve router + per-sleeve `SleeveBudget` + capital ceilings | §3, §7 |
| A3 | House risk gate v2 (15 named checks, `ALLOW`/`REDUCE`/`BLOCK` + `binding_gate`) | §4 |
| A4 | Orthogonal `opportunity_score` / `trade_permission` / `binding_gate` on every signal | §4.3 |
| A5 | Intraday engine: market-data service, day-type classifier, 5 setups, cost gate, time gates | §6 |
| A6 | One sizer + vol targeter + ES/CVaR engine + drawdown ladder | §7.2, §7.5, §10 |
| A7 | Order path: policy selector, OrderGuard, order manager (`client_order_id`, query-before-retry), `TradingStream` state machine, synthetic stops, halt/LULD machine, EOD flatten, reconciler | §8, §10 |
| A8 | Control API (signed) + MCP server (read/simulate/propose; mutate off by default) | §12.3, §12.4 |
| A9 | Allocation manager + scorecard/comparator + trial registry | §7.3, §11 |
| A10 | Test/drill expansion (hermetic, mutation, replay, 12 drills) | §13 |

### 0.3 Non-negotiables carried into every phase

Research is advisory · fail closed · no lookahead · deterministic numbers · one computation · no blind capital
competition · **the LLM proposes, the gate disposes, the human approves** · no code from the research repo, ever.
A phase is not "done" because it runs; it is done when §9's tests for that phase are green and the phase gate in
§8 is signed off.

---

## 1. Repo layout and the independence mechanics

### 1.1 Decision: extend `signald`, do not fork a second daemon

One engine, one ledger, one lock, one kill switch, mode-driven behaviour (the pattern already established by
`EXECUTION_IMPLEMENTATION_PLAN.md` §1). A second daemon would duplicate risk state — the exact failure this design
forbids. The order path stays unreachable unless `TRADINGEXEC_MODE ∈ {paper, live}` **and** `--execute` is passed.

```
TradingExecution/
├── signald/
│   ├── config.py            (+ sleeve/risk/intraday/api/mcp keys — §3)
│   ├── schema.py            (+ v2 contracts — §2)
│   ├── contracts.py         (NEW: envelope validate + version negotiation + dead-letter)
│   ├── mandate.py           (+ sleeve ceilings, approval thresholds, re-sign audit)
│   ├── inbox.py             (NEW: dedupe table = effectively-once ingest)
│   ├── watch.py             (unchanged behaviour, now feeds inbox)
│   ├── sleeves/
│   │   ├── __init__.py
│   │   ├── router.py        (NEW: decision/signal → sleeve)
│   │   ├── swing.py         (NEW: value-dip sleeve policy — §4)
│   │   ├── intraday.py      (NEW: day-trade sleeve policy — §5)
│   │   └── budgets.py       (NEW: SleeveBudget + ceilings + heat accounting)
│   ├── marketdata/
│   │   ├── service.py       (NEW: bars/quotes with as_of + feed + freshness)
│   │   ├── calendar.py      (NEW: sessions/half-days/halts; code-shipped)
│   │   ├── clock.py         (NEW: NTP offset check, 50 ms bound)
│   │   └── daytype.py       (NEW: opening drive / trend-range / gap bucket)
│   ├── signals/
│   │   ├── setups.py        (NEW: ORB, VWAP pullback/reversion, GAP_GO/GAP_FADE)
│   │   └── costgate.py      (NEW: expected-move vs k × round-trip cost)
│   ├── risk/
│   │   ├── gate.py          (NEW: 15 checks, verdict + binding gate)
│   │   ├── voltarget.py     (NEW: EWMA vol, target scalar, warm-up, cap)
│   │   ├── tail.py          (NEW: ES/CVaR parametric + EWMA + stress grid)
│   │   ├── ladder.py        (NEW: path-independent de-risking + kill)
│   │   └── sizing.py        (NEW: the single sizer, fractional-Kelly input)
│   ├── order/
│   │   ├── policy.py        (NEW: child-order policy per setup)
│   │   ├── guard.py         (NEW: pre-trade OrderGuard incl. wash/self-cross)
│   │   ├── manager.py       (NEW: client_order_id, query-before-retry, replace/cancel)
│   │   ├── state.py         (NEW: OrderStateManager over trade_updates)
│   │   ├── stops.py         (NEW: synthetic stop-limit, cluster proximity, MAE)
│   │   ├── halts.py         (NEW: LULD/halt state machine + cooldown)
│   │   └── flatten.py       (NEW: EOD flatten + flat verification)
│   ├── reconcile.py         (NEW: broker↔local, fills-not-targets)
│   ├── lineage.py           (NEW: allocation manager + scorecard + trial registry)
│   ├── api/
│   │   ├── server.py        (NEW: signed control API — read/propose/execute groups)
│   │   └── signing.py       (NEW: HMAC canonical string, nonce store, key roles)
│   ├── mcp/
│   │   ├── server.py        (NEW: MCP tools, read/simulate/propose; mutate off by default)
│   │   └── tools.py         (NEW: tool table + approval binding)
│   └── … (stores, notifier, kill_switch, daemon, cli, watchdog, samples, alpaca_ref)
├── contracts/               (NEW: *.schema.json — the published interface, duplicated by design)
├── docs/                    (DESIGN, this plan, USER_GUIDE, AGENT_ONBOARDING)
└── tests/                   (hermetic; mirrors the module tree)
```

### 1.2 Independence, enforced not promised

| Requirement | Mechanism | Test / gate |
|---|---|---|
| No research code | no `import tradingagents`; no vendored copy; no sys.path games | CI job: import-graph scan + a build with the sibling repo renamed to prove independence |
| No shared runtime | own venv, `py -3.12`, hash-pinned `requirements.txt` (`--require-hashes`, `--only-binary :all:`) | CI install job |
| Own keys | `TRADINGEXEC_*` only. **Remove** the `TRADINGAGENTS_ALPACA_*` prefix alias in `config.py` (`_TAGENT_ALPACA_PREFIX`) — it makes the research repo's env a supported input | config test asserting the alias is rejected |
| Own vendors | `alpaca-py` + the paid real-time data client + calendar package are execution-side deps | dependency test: no research-repo package in the lock file |
| Own `.env` | copy only the keys execution needs; keystore preferred; `config_hash` excludes secrets (already true) | test: secrets absent from `config_hash` and from logs |
| Fail-isolated | research repo absent → no new swing signals, **all risk checks still run** | test: run the suite with the sibling path missing |
| Contract drift | execution validates on ingest; unknown `MAJOR` → reject with a machine-readable error; a nightly probe validates one freshly generated research artifact | contract conformance test + probe job |

---

## 2. Contracts (v2) — the boundary is typed data, never prose

### 2.1 Envelope rules (both directions)

| Rule | Value |
|---|---|
| `schema_version` | semver `MAJOR.MINOR.PATCH`; `MAJOR` = breaking, `MINOR` = additive, `PATCH` = docs/fix |
| Compatibility | `BACKWARD_TRANSITIVE`: a consumer must read every prior producer within its supported `MAJOR` |
| Unknown envelope fields | ignored (`additionalProperties: true`) |
| Enums | **closed**: an unknown `action`, `direction`, `trade_permission`, or `binding_gate` is **rejected**, never coerced |
| Rejection | dead-letter directory + `{reason_code, detail}`; never partially applied |
| Expiry | mandatory `expires_at`; an artifact produced before the open is not actionable after the close |
| Idempotency | `idempotency_key` (UUIDv4) on every mutating request; retention ≥48 h (covers a manual retry across a session boundary) |
| Signature | all non-GET requests signed (§7.2) |
| Provenance | `producer{service, git_sha, run_id}` + `artifact_sha256`; the decision hash already pins the body |

### 2.2 `ResearchDecisionV1` — what the research layer must emit

Additive to today's `schema_version: 1` (current `ResearchDecision` keeps every field it has):

```json
{
  "schema_version": "1.1.0",
  "idempotency_key": "<uuidv4>",
  "produced_at": "2026-09-12T13:45:02Z",
  "expires_at": "2026-09-12T20:00:00Z",
  "producer": {"service": "tradingagents", "git_sha": "abc1234", "run_id": "<uuidv4>"},
  "ticker": "MSFT",
  "effective_date": "2026-09-12",
  "rating": "Overweight",
  "direction": "add",
  "opportunity_score": 84,
  "confidence": 0.72,
  "thesis": "…",
  "rationale": "…",
  "recommended_allocation_pct": 0.55,
  "position": {"target_notional": null, "stop_loss": 429.0, "take_profit": null, "size_pct_book": 0.0242},
  "data_quality": "fresh",
  "price_caliber": "adjusted",
  "invalidations": ["price_stop_loss: breach below 429.0"],
  "guardrail_reason": null,
  "risk_context": {"regime": "risk-on", "research_cvar_975_1d_pct": 1.1, "risk_gate": {"verdict": "PASS", "reasons": []}},
  "disclosure": {"sources_used": ["eodhd"], "sources_empty": []},
  "artifact_sha256": "<hex>",
  "decision_hash": "sha256:…"
}
```

Rules: `risk_context` is **advisory** and may never set `trade_permission`; `opportunity_score` is
producer-owned (0–100); unproducible fields are `null` and the artifact is not emitted (fail closed).

### 2.3 Field mapping — today → v2

| Today (`signald/schema.py`) | v2 | Change |
|---|---|---|
| `ResearchDecision.decision_hash` | same | unchanged (body hash, `decision_hash` excluded) |
| `ResearchDecision.risk_gate_verdict/reasons` | folded into `risk_context.risk_gate` | advisory-only, renamed |
| `ResearchDecision.extra` catch-all | explicit fields + `extra` retained | additive |
| `SignalContract.action` | same (BUY/HOLD/REDUCE/EXIT/NONE) | unchanged |
| `SignalContract.{score, confidence}` | `opportunity_score`, `confidence` | score becomes 0–100 and is producer-owned |
| — | `sleeve` (`swing`\|`intraday`) | new; set by the router, never by the producer |
| — | `trade_permission` (`ALLOW`\|`REDUCE`\|`BLOCK`) | new; **gate-owned** |
| — | `binding_gate`, `permission_reason_code`, `permission_reason` | new; gate-owned |
| — | `cost_gate {ok, expected_move_bps, round_trip_cost_bps, required_multiple}` | new |
| — | `day_type {rvol_first5, rvol_rank, adx_14, vwap_slope, gap_pct, gap_atr_bucket, opening_range_min}` | new (intraday) |
| — | `time_gate {entry_after, entry_before, flat_by}` | new (intraday) |
| — | `valid_until` | new; supersedes the static `expiry` for intraday |
| `SignalContract.stop_price`, `target_price` | same + `stop_kind` (`native`\|`synthetic_stop_limit`) | new field |
| `SignalContract.decision_hash` | `idempotency_key` + `decision_hash` | both retained |

### 2.4 Other records

| Record | Key fields |
|---|---|
| `GateDecision` | `id`, `intent_id`, `verdict`, `binding_gate`, `reasons[]`, `adjusted_risk_pct?`, `state_snapshot{equity, heat, es_975_1d_pct, drawdown_pct, sleeve_used_pct}`, `decided_at`, `config_hash` |
| `RiskRequest` | `sleeve`, `symbol`, `setup`, `direction`, `requested_risk_pct`, `stop_distance`, `measured_move_bps`, `created_at` |
| `OrderIntent` (paper/live only) | `intent_id`, `sleeve`, `symbol`, `side`, `qty`, `order_type`, `limit_price?`, `stop_price?`, `tif`, `client_order_id`, `parent_proposal_id?`, `created_at` |
| `Proposal` | `proposal_id`, `payload_hash`, `created_by` (`llm`\|`human`\|`sleeve`), `created_at`, `state` (`pending`\|`approved`\|`rejected`\|`expired`) |
| `Approval` | `approval_id`, `proposal_id`, `payload_hash`, `approved_by`, `approved_at`, `expires_at`, `single_use` |
| `SleeveBudget` | `sleeve`, `capital_ceiling_pct`, `vol_target`, `risk_per_trade_pct`, `es_budget_pct`, `daily_loss_soft/hard_pct`, `max_positions`, `max_trades_per_name`, `deployed_pct`, `heat_pct` |

### 2.5 Dead-letter and quarantine

`decisions/_dead_letter/` (schema-invalid, hash-mismatch, expired, unknown `MAJOR`) and
`signals/_quarantine/` (produced-but-not-permissible), each with a sidecar `.reason.json` carrying
`{reason_code, detail, received_at, producer}`. Failures are **never silent**: every rejection is an audit row and
a notifier `error` event.

---

## 3. Configuration — `TRADINGEXEC_*` (new keys with defaults)

Existing keys (paths, poll, ingest window, cooldown, cash reserve, ref-required, notifier, Alpaca paper) are
unchanged. Additions, all overridable in `.env` and all included in `config_hash` (secrets excluded):

| Group | Key | Default | Meaning |
|---|---|---|---|
| mode | `TRADINGEXEC_MODE` | `signal` | `signal` \| `paper` \| `live`; order path unreachable in `signal` |
| sleeves | `TRADINGEXEC_SLEEVE_SWING_CAPITAL_PCT` | 0.70 | hard ceiling, swing |
| | `TRADINGEXEC_SLEEVE_INTRADAY_CAPITAL_PCT` | 0.30 | hard ceiling, intraday |
| | `TRADINGEXEC_SLEEVE_SWING_VOL_TARGET` | 0.10 | annualized |
| | `TRADINGEXEC_SLEEVE_INTRADAY_VOL_TARGET` | 0.10 | annualized |
| | `TRADINGEXEC_INTRADAY_RESERVE_TO_SWING` | `false` | idle intraday capital never auto-flows to swing |
| risk | `TRADINGEXEC_RISK_PER_TRADE_INTRADAY_PCT` | 0.0025 | of house equity |
| | `TRADINGEXEC_RISK_PER_TRADE_SWING_PCT` | 0.005 | of house equity |
| | `TRADINGEXEC_MAX_HEAT_PCT` | 0.03 | open risk across the book |
| | `TRADINGEXEC_MAX_POSITIONS_INTRADAY` | 5 | |
| | `TRADINGEXEC_MAX_POSITIONS_SWING` | 8 | total ≤12 enforced |
| | `TRADINGEXEC_MAX_SINGLE_NAME_PCT_SWING` | 0.10 | of house equity |
| | `TRADINGEXEC_MAX_SINGLE_NAME_PCT_INTRADAY` | 0.05 | of house equity |
| | `TRADINGEXEC_MAX_ADV_PCT` | 0.25 | of 20-day ADV, per order |
| | `TRADINGEXEC_CLUSTER_CAP_PCT` | 0.25 | of sleeve notional per correlated cluster |
| | `TRADINGEXEC_ES_BUDGET_SLEEVE_PCT` | 0.015 | 1-day 97.5% ES of the sleeve |
| | `TRADINGEXEC_ES_BUDGET_HOUSE_PCT` | 0.010 | 1-day 97.5% ES of the book |
| | `TRADINGEXEC_ES_CONFIDENCE` | 0.975 | |
| | `TRADINGEXEC_ES_BACKTEST_DAYS` | 500 | |
| | `TRADINGEXEC_DAILY_LOSS_SOFT_PCT` | 0.01 | halve size |
| | `TRADINGEXEC_DAILY_LOSS_HARD_PCT` | 0.03 | flatten intraday |
| | `TRADINGEXEC_DERISK_5D_PCT` | 0.06 | rung 3 |
| | `TRADINGEXEC_DERISK_DD_PCT` | 0.10 | rung 3 |
| | `TRADINGEXEC_KELLY_FRACTION` | 0.25 | quarter-Kelly input, shrunk |
| | `TRADINGEXEC_LEVERAGE_CAP` | 1.5 | |
| | `TRADINGEXEC_VOL_HALFLIFE_DAYS` | 20 | EWMA |
| | `TRADINGEXEC_VOL_WARMUP_DAYS` | 270 | scaling disabled below this |
| | `TRADINGEXEC_VOL_SCALE_CAP` | 1.5 | hard cap on the scalar |
| | `TRADINGEXEC_VOL_REBALANCE_BAND` | 0.12 | act only beyond this relative move |
| | `TRADINGEXEC_STRESS_CORRELATION` | 0.85 | cluster stress |
| intraday | `TRADINGEXEC_INTRADAY_ENABLED` | `false` | opt-in |
| | `TRADINGEXEC_INTRADAY_ENTRY_AFTER` | `09:35` | ET |
| | `TRADINGEXEC_INTRADAY_ENTRY_BEFORE` | `11:00` | ET |
| | `TRADINGEXEC_INTRADAY_FLAT_BY` | `15:50` | ET |
| | `TRADINGEXEC_INTRADAY_MAX_TRADES_PER_NAME` | 1 | |
| | `TRADINGEXEC_OPENING_RANGE_MINUTES` | 5 | |
| | `TRADINGEXEC_RVOL_MIN` | 2.0 | first-5-min relative volume |
| | `TRADINGEXEC_RVOL_TOP_N` | 20 | rank gate |
| | `TRADINGEXEC_MIN_PRICE` | 5.00 | |
| | `TRADINGEXEC_MIN_ADV_SHARES` | 1000000 | 14-day |
| | `TRADINGEXEC_MIN_ATR` | 0.50 | 14-day |
| | `TRADINGEXEC_ADX_TREND` | 25 | |
| | `TRADINGEXEC_ADX_RANGE` | 20 | |
| | `TRADINGEXEC_STOP_ATR_MULT` | 1.75 | |
| | `TRADINGEXEC_STOP_MAE_PCTL` | 0.80 | MAE percentile floor for stop distance |
| | `TRADINGEXEC_COST_GATE_MULTIPLE` | 3 | expected move > k × round trip |
| | `TRADINGEXEC_COST_LIQUID_BPS` | 12 | round trip, liquid |
| | `TRADINGEXEC_COST_LOWFLOAT_BPS` | 30 | round trip, low-float |
| data | `TRADINGEXEC_DATA_FEED` | `sip` | `sip` \| `iex` (research/backfill may use `iex`) |
| | `TRADINGEXEC_MAX_QUOTE_STALENESS_S` | 2 | |
| | `TRADINGEXEC_MAX_BAR_STALENESS_S` | 60 | |
| | `TRADINGEXEC_CLOCK_MAX_OFFSET_MS` | 50 | FINRA 4590 |
| api | `TRADINGEXEC_API_ENABLED` | `false` | |
| | `TRADINGEXEC_API_BIND` | `127.0.0.1:8787` | loopback default |
| | `TRADINGEXEC_API_KEY_ID` | — | role-scoped |
| | `TRADINGEXEC_API_REPLAY_WINDOW_S` | 300 | |
| | `TRADINGEXEC_APPROVAL_TTL_S` | 900 | single-use token TTL |
| mcp | `TRADINGEXEC_MCP_ENABLED` | `false` | |
| | `TRADINGEXEC_MCP_BIND` | `127.0.0.1` | never `0.0.0.0` |
| | `TRADINGEXEC_MCP_TOOLSETS` | `read,simulate,propose` | mutating tools absent unless explicitly listed |
| lineage | `TRADINGEXEC_TRIAL_REGISTRY` | `./audit/trials.jsonl` | N is a first-class output |
| | `TRADINGEXEC_BACKUP_DIR` | `./backups` | |

Secrets: `TRADINGEXEC_ALPACA_KEY/SECRET` (or `ALPACA_*`), the data-vendor key(s), and the API signing secret.
Preferred storage is the OS keystore (Windows DPAPI/Credential Manager; Linux `systemd-creds`); `.env` is the
fallback for development only. Values are copied from the research layer **at setup time**, never read from it at
runtime (design §2.2).

---

## 4. Component catalogue

Every component below states its failure behaviour. The rule for all of them: **an exception is a `BLOCK`, never
a pass.**

### 4.1 Summary

| # | Component | Input → Output | Fail-closed behaviour |
|---|---|---|---|
| C1 | inbox + dedupe | artifact files → `Accepted(artifact)` / `Deduped` / `DeadLetter` | unreadable/invalid → dead-letter + audit |
| C2 | normalizer | artifact → `SignalContract` (pure) | unresolved action/hash mismatch → rejection |
| C3 | sleeve router | contract → `swing` \| `intraday` | unknown sleeve → rejection |
| C4 | market-data service | symbol → bars/quotes + `as_of` + `feed` + freshness | stale/missing → instrument quarantined for the cycle |
| C5 | calendar/clock | now → session state; NTP offset | session unknown or clock offset >50 ms → no new orders |
| C6 | day-type classifier | bars → `DayType` | insufficient data → no intraday signals |
| C7 | setup engine | `DayType` + bars → `SignalCandidate[]` | no data → no candidates |
| C8 | cost gate | candidate + cost model → `ok`/`reject(reason)` | cost unknown → reject |
| C9 | house risk gate | `RiskRequest` + book + market + mandate → `GateDecision` | any check erroring → `BLOCK(check)` |
| C10 | tail/ES engine | positions/returns → ES estimate + budget usage | insufficient history → conservative ES (stress grid), flagged |
| C11 | vol targeter | returns → scalar | warm-up unmet → scalar 1.0 (no scaling up) |
| C12 | ladder/kill | equity path → rung state | unknown state → halt new entries |
| C13 | sleeve budgets | book + ceilings → `SleeveBudget` usage | over ceiling → `REDUCE`/`BLOCK(sleeve_capital)` |
| C14 | sizer | `RiskRequest` + budgets + scalar → `RiskDecision(qty)` | non-positive/invalid → reject |
| C15 | order policy | candidate + regime → `OrderIntent` shape | ambiguous → conservative (limit, not market) |
| C16 | OrderGuard | `OrderIntent` + mandate + book → allow/deny | any check error → deny |
| C17 | order manager | `OrderIntent` → broker receipt + state events | timeout → query by `client_order_id`, then reconcile; never blind resend |
| C18 | stop manager | open position → protective order | protective order absent → flatten that position, page |
| C19 | halt/LULD machine | market state → entry permission | halt state unknown → no entries |
| C20 | EOD flatten | positions + clock → flat | failure to flatten → escalate, page, no "assume flat" |
| C21 | reconciler | broker vs local → drift | contradiction → HALT |
| C22 | ledger/journal/audit | every event | write failure → refuse to act (own-before-write) |
| C23 | allocation manager | scorecard → ceilings | insufficient evidence → no change |
| C24 | scorecard/comparator | fills + journal → metrics | <minimum sample → report "insufficient evidence" |
| C25 | API server | signed requests → responses | signature/role failure → generic 401/403, audited |
| C26 | MCP server | tool calls → results | unlisted tool → not found; mutate without approval → refused |
| C27 | watchdog/notifier | heartbeat + alerts | heartbeat stale → page + optional force-flatten |

### 4.2 The house risk gate (C9) — check order and precedence

Precedence is fixed; the **first** failing check is reported as `binding_gate`, all failures are reported as
`reasons[]`:

`mandate` → `sleeve_capital` → `house_drawdown` → `house_cvar` → `correlation_stress` → `vol_regime` →
`market_regime` → `knife_guard` → `concentration` → `liquidity` → `cost` → `wash` → `shortability` → `data` →
`time`/`halt` → `approval`.

`REDUCE` semantics: only `vol_regime`, `house_cvar`, `concentration`, `liquidity`, and `sleeve_capital` may
*reduce* rather than block; the reducing check is named and `adjusted_risk_pct` is returned. No other check may
silently shrink an order — a shrink that is not attributable is a bug.

Structural notes:

- **Wash/self-cross (G14)** is evaluated on the *projected post-trade book*, before submission: both sleeves hit
  the same symbol → the gate nets (allowed only if net exposure decreases overall) or blocks one side; the broker
  rejects self-crossing orders with HTTP 403, and a 403 is a gate failure, not a retry [L6][X9].
- **Knife guard (G7)** blocks mean-reversion entries against a strong adverse impulse, a fresh news shock
  (provenance-stamped), or within the halt cooldown.
- **Data (G13)** needs `data_quality == fresh` **and** the venue vintage within budget; mixed price calibers block
  the price-sanity check (never "assume adjusted").

### 4.3 Sizing (C14) — the only place a quantity is produced

```text
1. risk_pct      = min(per_trade_pct, sleeve_risk_remaining, house_heat_remaining)
2. risk_pct      = risk_pct × vol_scalar            # vol_scalar ∈ (0, 1.5]
3. risk_pct      = min(risk_pct, kelly_cap(edge, fraction))     # quarter-Kelly, shrunk
4. qty_risk      = (equity × risk_pct) / stop_distance
5. qty_liq       = participation_cap × trailing_volume
6. qty           = floor(min(qty_risk, qty_liq, sleeve_notional_room / price))
7. if qty < 1 → reject("size_below_one_share"); never round up, never widen a stop to fit size
```

- `kelly_cap` uses fractional Kelly (default quarter) with an estimation-error haircut; the *input* edge must come
  from the pre-registered validation record for that setup — an unvalidated setup's Kelly input is 0, so its size
  is 0.
- Swing and intraday requests go through the same function; the sleeve only supplies the policy knobs.
- Volatility scaling is applied with a band (act only on >12% relative change) to avoid turnover churn [R1].

### 4.4 Order path (C15–C21)

```mermaid
sequenceDiagram
    participant S as Setup/Sleeve
    participant G as House gate
    participant P as Order policy
    participant GD as OrderGuard
    participant OM as Order manager
    participant B as Alpaca
    participant ST as State (TradingStream)
    participant R as Reconciler
    S->>G: RiskRequest
    G-->>S: GateDecision(ALLOW/REDUCE/BLOCK, binding_gate)
    S->>P: sized intent
    P->>GD: OrderIntent (limit/marketable-limit, synthetic stop)
    GD->>GD: mandate · sleeve · wash · BP · precision · bracket legality
    GD->>OM: accepted intent
    OM->>OM: own-before-write (client_order_id)
    OM->>B: submit (marketable limit)
    B-->>ST: new → accepted → partial_fill → fill | rejected/canceled
    ST->>R: fills
    R->>R: fills vs targets; drift ⇒ HALT
    OM->>B: query by client_order_id on any timeout, never resend
```

Rules: entries are limit/marketable-limit (never market); a stop exists before or with the entry; brackets are used
only where legal (extended hours and notional/fractional constraints differ) [X9]; on any ambiguity the order is
**not** sent; a rejection is audited with the broker's machine reason.

---

## 5. Intraday setup specification (C5–C8)

### 5.1 Pipeline

```text
universe scan (pre-open)
   → day-type classifier at 09:35 (RVOL, opening range, ADX, VWAP slope, gap bucket)
   → setup eligibility (RVOL/rank/liquidity/regime)
   → cost gate (expected move > 3 × round trip)
   → RiskRequest → house gate → sizer → policy → OrderGuard → broker
   → stop managed, target/time exit, EOD flatten
```

### 5.2 Setup rules table (defaults; every threshold is a config key in §3)

| Setup | Eligibility | Entry | Stop | Exit | Cost-gate expectation |
|---|---|---|---|---|---|
| `ORB_RVOL` | price >$5, ADV >1M, ATR >$0.50, first-5-min RVOL ≥2.0, top-20 rank, ADX >25 | break of the 5-min range, 09:35–11:00 | max(1.75×ATR(14), 80th-pct MAE) | 10R base or trailing; flat by cutoff | expected move ≥3× round trip |
| `VWAP_PULLBACK` | ADX >25, trend day, price holds one side of a sloping VWAP | pullback that holds VWAP/AVWAP | as above | prior swing · VWAP breach · time stop | same |
| `VWAP_REVERT` | ADX <20, range day, stretch to outer band | resting limit at/below VWAP (never market) | as above | revert to VWAP | same |
| `GAP_GO` | gap ≥4%, RVOL ≥3×, price > first-15-min high, rising MACD | marketable limit on the break | last swing low −$0.01 or −5% | 3R + MACD exit | same |
| `GAP_FADE` | \|gap\| <0.3× ATR, RVOL context, not a news shock | resting limit | above the opening high | prior close | same |

Blocked by construction: raw index-ETF ORB; generic HOD/LOD breakouts; overnight-holding intraday variants;
first-half-hour→last-half-hour momentum. Rationale with numbers: design §6.3 [A1][A3][A5][A8].

### 5.3 Time and halt gates

| Gate | Value |
|---|---|
| earliest entry | 09:35 ET (spread ≈24 bps at the open vs ≈13 bps by 09:40) |
| latest entry | 11:00 ET (mechanical) |
| flat by | 15:50–15:55 ET, verified against the broker |
| no entries | during a limit/halt state and for 5 minutes after the reopen cooldown |
| last-10-minute halt | no reopen occurs → position managed by the closing procedures; no new orders |
| clock | offset >50 ms → no new orders (FINRA 4590) |

---

## 6. Formulas (copyable, with defaults)

```text
# 1. Volatility (EWMA, zero-mean, 20-day half-life, 5-min bars aggregated to daily)
lambda = ln(2) / halflife
var_t  = (1-lambda) * sum(lambda^k * r_{t-k}^2)          # requires >= 270 trading days before scaling

# 2. Vol-target scalar (bounded; scaling up is capped, scaling down is unbounded above 0)
scalar = clamp(target_vol / realized_vol, 0.10, vol_scale_cap=1.5)
apply  = scalar if |scalar - current| / current > band(=0.12) else current

# 3. Fixed-risk position size
qty = floor( (equity * risk_pct) / stop_distance )
stop_distance = max(atr_mult * ATR14, MAE_pctl80(setup))

# 4. Fractional Kelly (input, not a sizer on its own; shrink on estimation error)
f_star  = p/l - q/g            # binary form: p win prob, l loss size, g gain size, q=1-p
f_used  = kelly_fraction * f_star * shrink(se)      # kelly_fraction default 0.25

# 5. Expected shortfall / CVaR at 97.5% (three estimators, take the worst)
es_hist   = mean(worst 2.5% of 1-day losses, window >= 500d)
es_ewma   = z_975 * ewma_sigma * sqrt(h)            # parametric, h = horizon (1 day)
es_stress = es_ewma scaled by stress_grid{ gap, halt, vol x2 }
es_used   = max(es_hist, es_ewma, es_stress)        # conservative by construction

# 6. Impact (pre-trade estimate; whole-order)
impact_bps = 10_000 * Y * sigma_daily * sqrt(Q / V)      # Y ≈ 0.5-1.0; Q = order size, V = ADV

# 7. Cost gate
require:  expected_move_bps > cost_gate_multiple(=3) * round_trip_cost_bps
round_trip_cost_bps = spread_bps + impact_bps + fees_bps(liquid=12 / low-float=30 default)

# 8. Risk of ruin (capital adequacy per sleeve, fixed-fractional)
A   = p*R - (1-p)                                        # R = avg win / avg loss
RoR ≈ ((1-A)/(1+A))^N                                    # N = capital / risk_per_trade (in risk units)

# 9. De-risking ladder (path-independent)
rung = first of [ (-1% day -> size x0.5), (-3% day -> flat intraday),
                  (-6% 5d | -10% DD -> both sleeves x0.5 + freeze allocation),
                  (-15% DD -> halt + manual re-arm) ]

# 10. Equal-risk-contribution sleeve weights (2 sleeves, correlation rho)
w1 = (1/sigma1) / (1/sigma1 + 1/sigma2)     # rho-independent for the 2-asset inverse-vol case
# 10% vs 30% vol -> 75/25 risk weights; with 70/30 capital the intraday sleeve holds ~61% of variance

# 11. Validation gates
DSR  >= 0.95     PBO <= 0.05     trades/setup >= 50     OOS >= 6 months    N (trials) logged
MinBTL ≈ 2*ln(N) / (SR*)^2
paired sleeve test: stationary bootstrap on the daily PnL difference; report the interval, not the point

# 12. Drawdown recovery expectation (re-entry discipline)
E[recovery time] ≈ 3 x drawdown-building time            # Triple Penance
```

Every formula above is implemented **once**, in the module named in §4.1, and is unit-tested against a hand-worked
example recorded in the test file. No sleeve, gate, or report recomputes any of them independently.

## 7. Control API and MCP surface

### 7.1 Endpoints (role-scoped; deny by default)

| Group | Endpoint | Role | Notes |
|---|---|---|---|
| read | `GET /v1/state/account` | `read` | cash, equity, buying power, IML/IMD state |
| read | `GET /v1/state/positions` | `read` | from fills + broker reconcile |
| read | `GET /v1/state/risk` | `read` | ES/CVaR usage, heat, drawdown rung, vol scalar |
| read | `GET /v1/state/sleeves` | `read` | per-sleeve ceiling, deployed %, PnL, scorecard summary |
| read | `GET /v1/signals/latest?symbol=` | `read` | last envelope per symbol |
| read | `GET /v1/orders/open` | `read` | working orders + protective legs |
| propose | `POST /v1/propose` | `propose` | creates an immutable proposal (hash-pinned); no broker effect |
| simulate | `POST /v1/simulate` | `propose` | full gate evaluation, no side effects |
| execute | `POST /v1/orders/submit` | `execute` | requires `proposal_id` + `approval_id`; hash must match |
| execute | `POST /v1/orders/cancel` | `execute` | approval token required |
| execute | `POST /v1/halt` | `execute` | always permitted; cancels + flattens |
| admin | `POST /v1/sleeves/{sleeve}/ceiling` | `admin` | operator approval + audit |

### 7.2 Request signing (all non-GET)

```text
canonical = METHOD "\n" PATH "\n" SHA256_HEX(body) "\n" X-Timestamp "\n" X-Nonce "\n" X-Key-Id
X-Signature = HEX(HMAC-SHA256(signing_secret, canonical))
```

| Item | Value |
|---|---|
| Timestamp | RFC 3339 UTC; host clock NTP-synced |
| Replay window | ±150 s (300 s total) |
| Nonce | 128-bit random hex, single-use, TTL 600 s, **inside the signed material** |
| Verify order | window → nonce unused → constant-time HMAC → consume nonce (single transaction) |
| Failure | generic `401`, never disclosing which check failed; audited either way |
| Keys | `X-Key-Id` → role (`read`/`propose`/`execute`/`admin`); stored in the OS keystore; rotated ≤12 months, two keys valid during overlap |
| Abuse controls | per-key rate limits (read 60/min, mutating 10/min), payload caps (1 MiB), request/broker timeouts (10 s), per-day broker-call budget, IP allow-list on `/v1/orders/*` |

### 7.3 MCP tools (read + simulate + propose by default)

| Tool | Class | Hints (`readOnly`/`destructive`/`idempotent`/`openWorld`) | Approval |
|---|---|---|---|
| `get_account_state`, `get_positions`, `get_open_orders`, `get_signal_latest`, `get_risk_state`, `get_sleeve_allocation` | read | true / n-a / true / false | no |
| `simulate_order` | dry-run | true / false / true / false | no |
| `propose_order` | write-intent | false / false / false / false | no |
| `submit_order(proposal_id, approval_id)` | mutating | false / true / false / true | **yes** + gate verdict |
| `cancel_order` | mutating | false / true / true / true | yes |
| `halt_trading` | safety | false / false / true / false | always permitted |
| `set_sleeve_allocation` | config | false / true / true / false | **yes**, operator only |

Rules: annotations are **untrusted hints**; the server-side allow-list is the control [I2]. No credentials or
secrets transit as tool arguments. Tool descriptions are hash-pinned and diffed on connect (anti rug-pull). Local
servers bind `127.0.0.1` and validate `Origin`. Every tool call produces a hash-chained audit row.

### 7.4 Proposal → approval → submit

```mermaid
sequenceDiagram
    participant A as LLM/agent
    participant M as MCP server
    participant G as House gate
    participant H as Operator
    participant B as Broker
    A->>M: propose_order(symbol, side, intent, rationale)
    M->>M: schema check; free text stored audit-only
    M->>G: simulate (no side effects)
    G-->>M: verdict + binding_gate + projected ES/drawdown
    alt verdict == BLOCK
        M-->>A: refused(verdict, binding_gate)   # no proposal exists
    else verdict in {ALLOW, REDUCE}
        M->>M: write Proposal(proposal_id, payload_hash, state=pending)
        M-->>H: approval request (agent-visible summary + hash)
        H->>M: approve(proposal_id) -> Approval(single-use, TTL 900s, bound to hash)
        M->>B: submit_order(proposal_id, approval_id)  # hash must match
    end
```

An LLM can never enlarge a size, change a stop, or resubmit after approval: the approval is bound to the proposal
**hash**, and a modified proposal is a different proposal.

---

## 8. Phases and gates

Nothing after P1 is enabled before the phase that precedes it is signed off. Every phase ships **off by default**.

| Phase | Deliverable | Exit criteria (all must hold) | Must stay disabled |
|---|---|---|---|
| **P0 — Boundary & contracts** | `contracts/*.schema.json`; envelope validate/version/dead-letter; inbox + dedupe; config v2 keys; alias removal; CI independence job | contract tests green (unknown MAJOR rejected, expiry enforced, enums closed); dedupe proven on replay; independence job passes with the sibling repo renamed | everything: `MODE=signal`, no order path |
| **P1 — Sleeves, gate, budget (signals only)** | sleeve router; `SleeveBudget`; house gate v2 (15 checks, binding gate); `opportunity_score`/`trade_permission` on the envelope; ES/vol/ladder engines; scorecard skeleton | gate table tests (each check blocks when it should, `REDUCE` names its check); ES/vol formulas match hand-worked examples; envelope carries the two orthogonal fields; `signals.jsonl` unchanged in shape for existing consumers (additive only) | order path, intraday engine |
| **P2 — Paper order path** | policy selector, OrderGuard, order manager, `TradingStream` state machine, synthetic stops, reconciler, `--execute` gate | paper submit→fill→reconcile drill with query-before-retry; duplicate-submission drill yields exactly one order; partial-fill + restart drill; SIGKILL drill; no naked stop-market in the code path | intraday sleeve, MCP mutate, live |
| **P3 — Intraday sleeve in paper** | market-data service (SIP requirement), calendar/clock, day-type classifier, 5 setups, cost gate, time/halt machines, EOD flatten | one full paper session: scan → signals → gated orders → flat by 15:55 → reconciled; stale-data and halt drills pass; cost gate blocks a deliberately uneconomic setup; `flat_by` verified against the broker | MCP mutate, live, allocation changes |
| **P4 — Validation & comparison harness** | trial registry, backtest-with-costs harness sharing the same risk/size code, comparator, scorecard report | 2026-01-02→2026-09-11 both sleeves on the same universe/capital/costs, trade-for-trade; DSR/PBO recorded with N; paired bootstrap interval reported; documented as **feasibility, not superiority** (§10.3) | live |
| **P5 — Guarded live** | tiny-capital live profile, two independent opt-ins, approval mode, RTO/RPO drill, runbook | promotion checklist (§11.4) complete; kill switch tested live; reconciliation clean for N sessions; max notional hard-capped | MCP mutate (unless explicitly approved), allocation changes |
| **P6 — 3–6 month parallel review** | scorecard + pre-registered kill/shrink decision | decision executed mechanically per design §7.6 | — |

Each phase's PR must include: the tests it adds, one mutation proof per new gate (the test fails when the gate is
neutered), and a CHANGELOG entry.

---

## 9. Testing, drills, and CI

### 9.1 Hermetic rules (non-negotiable)

| Rule | Enforcement |
|---|---|
| No network in tests | injected transport seam (already the pattern in `alpaca_ref.py`); non-loopback socket guard that **refuses and records**; teardown assertion that nothing dialed out |
| No broker credentials in CI | test asserts `APCA_*`/`ALPACA_*`/`TRADINGEXEC_ALPACA_*` are absent; CI sets none |
| Recorded fixtures only | HTTP/WS sessions recorded once and replayed; `trade_updates` fixtures drive the state machine |
| Paper by default in code | the default base URL is paper; live requires an explicit named opt-in that CI never sets |
| Time is injected | `now_fn` seam (already present); no test depends on wall-clock time |
| Determinism | fixed seeds; no test writes outside `tmp_path` |

### 9.2 Test taxonomy

| Class | What it proves |
|---|---|
| Contract | version negotiation, unknown-field/enum policy, expiry, hash pinning, dead-letter reasons |
| Gate | every one of the 15 checks blocks when it should; precedence; `REDUCE` names its check; no check can be bypassed by omitting an input |
| Property-based | sizing/gate invariants across randomized books (e.g. "size never exceeds the sleeve ceiling", "a stop always exists before submission", "exposure is never computed from targets") |
| Mutation | each gate test must fail when the gate is neutered (one proof per gate, recorded in the PR) |
| Replay | a recorded session reproduces the same signals/decisions deterministically |
| Order lifecycle | golden files for `new → accepted → partial → filled`, `rejected`, `canceled`, `replaced`, `expired` |
| Reconciliation | fills-vs-targets drift detection; contradiction → HALT |
| Audit | tamper a row → `verify` reports the first bad index; the chain is re-verified after every write batch |
| Cost model | hand-worked slippage/impact examples; paper-vs-model divergence recorded |

### 9.3 Drills (run before any promotion; one per week on rotation afterwards)

| # | Drill | Pass condition |
|---|---|---|
| D1 | cold start with open positions | positions reconstructed from the broker; zero drift; no order submitted |
| D2 | SIGKILL mid-session with a working order | restart detects the in-flight `client_order_id`, queries before retry, no duplicate |
| D3 | broker outage (5-minute 5xx/timeout) | no orphaned orders; jittered backoff; recovery reconciles; one page |
| D4 | duplicate submission | exactly one broker order; the second is recognised as a duplicate |
| D5 | partial fill then restart | residual tracked; stops re-based on the filled quantity |
| D6 | stale data (freeze the feed, advance the clock) | staleness alarm; new entries blocked; exits still allowed |
| D7 | clock event (step 2 s, slew, leap second) | offset alarm within 60 s; ledger timestamps never go backwards |
| D8 | ledger tamper | chain break detected; submissions disabled; page |
| D9 | backup/restore onto a clean host | measured RTO/RPO recorded; zero drift after restore |
| D10 | MCP/LLM red team (poisoned tool description, injected headline) | no proposal escapes the schema; no exfiltration; regressions block release |
| D11 | sleeve collision (both sleeves want the same symbol, opposite sides) | blocked/netted **pre-submission**; no broker 403 |
| D12 | no-broker CI | full suite passes with credentials absent and outbound access denied |

### 9.4 Alerts (day one)

`data_stale` (>2 s quotes / >60 s bars) · `spread_blowout` (>3× median) · `halt_entered` · `duplicate_client_order_id`
· `protective_stop_missing` · `reconcile_drift` · `daily_loss_soft` / `daily_loss_hard` · `derisk_rung` ·
`kill_switch` · `clock_offset` (>50 ms) · `broker_error_storm` (≥3 failures/60 s) · `gate_exception` (any check
raising) · `sleeve_ceiling_hit` · `flat_verification_failed` (15:55+) · `heartbeat_loss` (watchdog). Pageable rows
must be actionable; anything firing weekly without a novel cause is automated or deleted [L7].

---

## 10. Scorecard and the fair comparison

### 10.1 Metric spec (per sleeve, per setup, and combined; all net of costs and fees)

| Metric | Definition | Why it is here |
|---|---|---|
| **Return / Max Drawdown** | net return ÷ max drawdown | headline comparison (owner's brief) |
| Net CAGR | annualized net PnL ÷ average equity | absolute result |
| Sharpe / Sortino | mean/σ and mean/downside-σ of daily returns, annualized | risk-adjusted; Sortino for asymmetric intraday PnL |
| Calmar / MAR | CAGR ÷ max DD | drawdown-adjusted |
| Max Drawdown · Ulcer index | peak-to-trough; RMS of drawdown depth | tail of the equity curve |
| Profit Factor | gross wins ÷ gross losses | |
| Win rate · Avg win / Avg loss · Expectancy per trade | in R and USD | the payoff profile that ORB vs reversion differ in |
| Turnover | round trips/yr and notional traded ÷ equity | the cost engine's input |
| Slippage vs model | realized fill − modelled price, bps | the execution-truth metric |
| Commission + fees | SEC/TAF/CAT/commission, bps of notional | |
| Exposure · Capital utilization | % of sleeve capital deployed, time-weighted | capital efficiency, and whether a ceiling is binding |
| CVaR / ES (1-day 97.5%) | tail loss | the risk budget's own currency |
| Tail ratio | 95th / 5th percentile of daily returns | fat-tail asymmetry |
| Capacity | size at which modelled edge → 0 | prevents scaling into a wall |

### 10.2 Log schema (one row per trade, both sleeves)

`{sleeve, symbol, setup, entry_ts, exit_ts, qty, entry_px, exit_px, slippage_bps, fees_usd, r_multiple,
rvol_first5, adx_14, gap_pct, spread_bps, exit_reason, gate_verdict, binding_gate, data_vintage}`

This row is what makes the comparison *trade-for-trade* and what decomposes alpha between **signal** and
**execution** (design §11).

### 10.3 Comparison protocol (2026-01-02 → 2026-09-11)

| Item | Rule |
|---|---|
| Universe | the mandate allow-list, identical for both sleeves |
| Capital | identical starting equity; per-sleeve ceilings as configured (70/30) |
| Costs | the same cost model, applied identically (spread + impact + fees + borrow) |
| Data | same point-in-time bars/quotes for both sleeves |
| Output | both sleeves' §10.1 tables + the paired difference (stationary bootstrap interval) + correlation and co-drawdown statistics |
| Claim limit | **no winner is declared** unless the pre-registered statistical bar is met; with ≈175 sessions and Sharpe ≈0.9 vs 1.6, t=2 needs ≈3,500 sessions, so the honest conclusion is feasibility + cost drag + operational defects [V1][V2] |
| Deliverable | `signals/scorecard/2026-09-11.md` + `.json`, committed, with the trial count N and the DSR/PBO for every variant tried |

---

## 11. Runbook

### 11.1 Daily lifecycle (ET, RTH)

| Time | Action |
|---|---|
| 08:00 | pre-open: session calendar, NTP offset, data connectivity, account/buying power, IML/IMD state, mandate validity, kill switch armed, heartbeat |
| 09:00 | universe scan + RVOL baseline; swing sleeve evaluates any new research artifacts (entries scheduled per the no-lookahead rule) |
| 09:35 | day-type classification; intraday eligibility set (RVOL/rank/liquidity/regime) |
| 09:35–11:00 | intraday entry window; every order passes gate → sizer → OrderGuard |
| 11:00–15:45 | manage only: stops, targets, time stops; no new intraday entries |
| 15:45–15:55 | flatten intraday; verify flat against the broker; closing-auction preference where eligible |
| 16:15 | post-close: reconcile fills vs positions vs journal; compute the day's scorecard; write the audit summary |
| 17:00 | report: sleeve PnL, slip vs model, gate histogram (which gate blocked how often), data-quality summary |

### 11.2 Incident runbook

| Alert | First action |
|---|---|
| `gate_exception` | the order is already blocked; fix and re-run the gate test suite before re-enabling |
| `reconcile_drift` | HALT the sleeve, do not "fix" the local ledger to match; investigate the fill stream |
| `protective_stop_missing` | flatten that position, then investigate the order-state machine |
| `flat_verification_failed` | use the closing auction / marketable limit; escalate until verified flat; page |
| `clock_offset` | no new orders; fix NTP; verify the ledger has no backwards timestamps |
| `data_stale` | quarantine the instrument; if it is systemic, stop new intraday entries for the session |
| `broker_error_storm` | kill switch; reconcile after the broker is healthy |
| `daily_loss_hard` | flatten intraday; no re-entry; post-mortem written before re-arm |

### 11.3 Kill switch and re-arm

`halt_trading` (MCP or CLI) → cancel working orders → flatten intraday → freeze new entries → write the episode
latch. Re-arm is **manual only**, requires a written post-mortem reference, and restores size in steps
(25% → 50% → 100% across sessions, subject to the vol target and the ladder).

### 11.4 Promotion checklist (paper → live, tiny)

- [ ] ≥20 paper sessions, all reconciled, all flat-by-close, zero unexplained drift.
- [ ] D1–D12 drills passed, with the drill log committed.
- [ ] Validation gates met for every enabled setup (DSR/PBO/trades/OOS) with N recorded.
- [ ] Cost model validated against ≥50 realized paper fills (slippage distribution, not a point estimate).
- [ ] Kill switch, daily-loss flatten, and stale-data block each demonstrated live (paper).
- [ ] Hard capital cap configured; approval thresholds set; two independent opt-ins in place.
- [ ] Backups + restore drill done; alerting verified; runbook printed/committed.
- [ ] Owner signs the pre-registered kill/shrink criteria date.

---

## 12. Traceability

| Requirement (design) | Components | Proving tests |
|---|---|---|
| D5 separation / no shared code (§2.2) | repo layout, CI job | independence job with the sibling renamed; import-graph scan |
| D4 opportunity vs permission (§4.3) | C2, C9, envelope | gate tests: high `opportunity_score` + blocked book → `BLOCK(binding_gate)`; low score + clean book → `ALLOW` |
| D2/D5 no blind capital competition (§3.5, §7) | C3, C13, C14, C23 | property test: sum of sleeve notional ≤ ceilings at all times; same-symbol collision drill D11 |
| House gate (§4.2) | C9 | one blocking test per check + precedence test + mutation proof per check |
| Fail-closed everywhere (§1.2) | all | fault-injection: each component raises → `BLOCK`, never a pass |
| No lookahead (§1.2) | C1, C4, C6, C7 | PIT replay test: decisions never read a later `as_of`; fills model next tradable price |
| Order safety (§4.4, §8) | C15–C21 | D2/D4/D5/D11, golden lifecycle files, query-before-retry test |
| Cost realism (§8.1) | C8, C14, C24 | hand-worked impact/slippage tests; cost gate blocks an uneconomic setup |
| Validation rigour (§11) | C24, trial registry | DSR/PBO/MinBTL unit tests on synthetic series; N recorded in every scorecard |
| LLM cannot act (§12) | C25, C26 | D10; schema test: no numeric field in the LLM-authored record; approval binding test |
| Audit integrity (§12.5) | C22 | tamper test on `audit.jsonl`/chain; every mutation has an audit row |

---

## 13. Open items (owner decisions before P1 is built)

1. Capital ceilings and risk budgets (design §15.1) — confirm 70/30 and the ES budgets.
2. Real-time data subscription purchase (required for P3; free tiers are not decision-grade).
3. Universe and shorting for the intraday sleeve.
4. Session cadence for the swing sleeve: how often does the research layer run, and does it publish an
   `opportunity_score` on every run?
5. §475(f) election and wash-sale bookkeeping across sleeves (2026 is time-barred; decide for 2027).
6. Kill/shrink criteria and the review date (design §7.6).
7. Whether `signald` stays one process (§1.1) after live, or splits into `orderd` on an isolated host.
8. Paper vs live broker keys: confirm separation and rotation policy.

---

## Appendix A — Work list (new files, one line each)

| File | Purpose |
|---|---|
| `contracts/research_decision.v1.schema.json` | published producer contract (validated on ingest) |
| `contracts/signal.v2.schema.json` | envelope emitted by execution |
| `contracts/order_intent.v1.schema.json` | internal order contract (paper/live) |
| `signald/contracts.py` | envelope validation, version negotiation, dead-letter |
| `signald/inbox.py` | dedupe table + effectively-once ingest |
| `signald/sleeves/router.py · swing.py · intraday.py · budgets.py` | sleeve policy and ceilings |
| `signald/marketdata/service.py · calendar.py · clock.py · daytype.py` | execution-owned data, time, session, day type |
| `signald/signals/setups.py · costgate.py` | the five setups and the cost gate |
| `signald/risk/gate.py · voltarget.py · tail.py · ladder.py · sizing.py` | house gate, ES, vol target, ladder, one sizer |
| `signald/order/policy.py · guard.py · manager.py · state.py · stops.py · halts.py · flatten.py` | the order path |
| `signald/reconcile.py` | broker↔local reconciliation |
| `signald/lineage.py` | allocation manager, scorecard, trial registry |
| `signald/api/server.py · signing.py` | signed control API |
| `signald/mcp/server.py · tools.py` | MCP surface with approval binding |
| `tests/test_contracts*.py`, `test_gate_*`, `test_risk_*`, `test_order_*`, `test_intraday_*`, `test_api_*`, `test_mcp_*`, `test_scorecard.py`, `tests/drills/` | the proving suite |

## Appendix B — Research corpus

The evidence behind every threshold in these two documents was gathered on 2026-09-12 and is committed alongside
them under [`docs/research/`](research/). Each brief carries numbered sources with title, URL and date:

| File | Slice | Bytes |
|---|---|---|
| [`01_intraday_alpha.md`](research/01_intraday_alpha.md) | ORB / intraday momentum / VWAP / gaps / RVOL / overnight-vs-intraday decomposition | 32,158 |
| [`02_execution_algorithms.md`](research/02_execution_algorithms.md) | Almgren-Chriss, square-root impact, child-order policy, stop slippage, auctions | 43,598 |
| [`03_intraday_risk.md`](research/03_intraday_risk.md) | vol targeting, ES/CVaR, Kelly shrinkage, stops/MAE, drawdown ladder, LULD, margin | 29,621 |
| [`04_backtest_validation.md`](research/04_backtest_validation.md) | DSR/PBO/CSCV, purged CV, MinBTL, paired comparison, cost-inclusive validation | 43,529 |
| [`05_microstructure_regulatory.md`](research/05_microstructure_regulatory.md) | SIP vs IEX, LULD, Reg SHO/SSR, T+1, FINRA 4210 IML/IMD, auctions, tax | 36,927 |
| [`06_alpaca_2026.md`](research/06_alpaca_2026.md) | Alpaca order/TIF/class matrix, paper-vs-live gaps, buying power, data plans, MCP, `alpaca-py` | 41,246 |
| [`07_isolation_mcp_security.md`](research/07_isolation_mcp_security.md) | service boundary + contracts, request signing, MCP 2026 spec, prompt injection, SBOM/hygiene | 46,564 |
| [`08_capital_allocation.md`](research/08_capital_allocation.md) | ERC/HRP, multivariate Kelly, correlation instability, throttling, sleeve scorecards | 27,457 |
| [`09_daytrading_economics.md`](research/09_daytrading_economics.md) | retail/prop profitability base rates, cost floors, decay, honest return ceilings | 24,003 |
| [`10_llm_ops.md`](research/10_llm_ops.md) | LLM-as-filter evidence, OWASP/NIST guardrails, prompt-injection incidents, daemon ops, drills | 77,801 |

Numbers quoted **without** a bracketed key in these documents are defaults chosen for this system, not sourced
claims; the bracketed keys resolve in the design document's Appendix B, and the corpus carries the full URL/date
for each. The briefs are research notes with `PROVEN / REPORTED / CONTESTED / INFERENCE` labels — where two briefs
disagree, the disagreement is stated in the design document rather than averaged away (see the fee-schedule note in
design §8.1).

Anything in this plan can be revised by evidence; nothing in it may be revised by a drawdown.

