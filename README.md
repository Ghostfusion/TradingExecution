# TradingExecution

Execution layer for the research-to-execution stack. Consumes deterministic
research decisions from the **TradingAgents** research layer, applies
fail-closed mandate gates, and produces audit-ready **signals** (Phase A) —
later, bounded orders at **Alpaca paper** (M1+, explicit opt-in). Never an
order without a mandate; research is advisory, execution is committed.

> ⚠️ Not financial advice. Phase A emits signals only — no vendor order is ever
> placed from this repo until the owner explicitly opts in (and live trading
> requires two independent opt-ins).

## Status

- **P0–P6 of the two-sleeve build implemented (1033 hermetic tests green, ruff
  clean):** on top of the Phase-A signal daemon — the versioned boundary
  (contracts + inbox + dead-letter), the sleeve router and budgets, the 16-check
  house risk gate with precedence and `binding_gate`, one sizer, the full paper
  order path (policy → guard → manager → state → synthetic stops → halts →
  flatten → reconcile), the intraday sleeve (calendar/clock, market-data service,
  day-type classifier, five setups, cost gate), the validation harness and
  lineage, the signed control API and the MCP surface, plus a runbook and a
  promotion checklist.
- **Every phase ships off by default.** Default `TRADINGEXEC_MODE=signal` has no
  order path at all; paper/live additionally require `execute=True`. The live
  broker/market-data adapters are not written yet (P5 deployment work) — the
  session engine takes injected seams. Nothing was enabled by this change.
- Design contract: [`../EXECUTION_IMPLEMENTATION_PLAN.md`](../EXECUTION_IMPLEMENTATION_PLAN.md)
  (TradingNew level) built on [`../Master_deign.md`](../Master_deign.md).
- **Two-sleeve algo intraday design** (value-dip swing + day-trade sleeve under one house risk gate, research↔execution
  separation, `opportunity_score` vs `trade_permission`): [`docs/INTRADAY_ALGO_DESIGN.md`](docs/INTRADAY_ALGO_DESIGN.md)
  with the build plan in [`docs/INTRADAY_ALGO_IMPLEMENTATION.md`](docs/INTRADAY_ALGO_IMPLEMENTATION.md).
  Both are design/plan only — they change no behaviour and enable nothing.

## Usage (Phase A)

```bash
py -3.12 -m signald init-mandate --mandate ./mandate.json   # signed mandate
py -3.12 -m signald sample --out ./decisions/AVGO_decision.json   # demo artifact
cp .env.example .env   # set ALPACA_API_KEY/ALPACA_SECRET_KEY (paper) + optional webhook
py -3.12 -m signald run --watch ./decisions --data ./signals   # dry-run (default)
py -3.12 -m signald run --execute --watch ./decisions --data ./signals   # persist signals
py -3.12 -m signald status --watch ./decisions --data ./signals
py -3.12 -m signald verify --audit ./signals/audit/audit.jsonl   # tamper check
```

**Defaults selected** (plan §7 open questions, owner hasn't overridden):
market-closed → *tag*, don't block · cooldown 12 h · `min_cash_reserve_usd`
25k · watch root `decisions/` · notifier off until a webhook URL is set ·
supervision = `signald run` (hub-managed in dev).

## Execution modes (config-driven, one engine)

| Mode | Behavior | Phase |
|---|---|---|
| `signal` | Normalize → gates → **notify only** (no order intent) | **Phase A (default)** |
| `paper` | Same pipeline → Alpaca **paper** orders → order-state events → portfolio/journal | M1+ (opt-in: `--execute`) |
| `live` | Same pipeline → Alpaca live | M3 (signed mandate + explicit flag) |
| `approval` | Gates pass → WAIT_FOR_APPROVAL → operator approve/reject | M3 bridge |

The research/analysis layer never knows which mode is active.

## Safety invariants (never compromised)

1. **Research is advisory, execution is committed** — no research output places an order.
2. **Fail closed** — any gate that cannot run → no signal/order.
3. **No lookahead** — signals bind to `effective_date`; reference prices are PIT.
4. **Deterministic** — gates/sizes/calibers are pure functions; the daemon never parses prose.
5. **Dry-run default** — logs what WOULD be emitted; `--execute` opts into paper.

## Project layout

```
TradingExecution/
├── README.md  CHANGELOG.md  docs/AGENT_ONBOARDING.md
├── contracts/      (published JSON Schemas: research_decision.v1, signal.v2,
│                    order_intent.v1 — the boundary is typed data)
├── signald/        (watch · normalizer · processor · one house risk gate ·
│                    sleeves · marketdata · signals · order path · engine ·
│                    reconcile · lineage · api · mcp · promotion · CLI)
│                    risk/ tail · voltarget · ladder · sizing · gate (precedence)
│                    order/ prices · policy · guard · manager · state · stops ·
│                    halts · flatten
│                    validation/ stats (DSR/PBO/bootstrap) · harness (replay)
├── tests/          (hermetic — injected seams, injected clock, zero network)
└── .github/        (CI: test + independence jobs)
```

Parent workspace:
- **TradingAgents** — multi-agent LLM research framework (reports, decisions). Emits `research_decision.json` (the daemon's only contract).
- **trading_web** — FastAPI + React web app; future read-only `GET /api/signals`.

## Environment

- Python: `py -3.12` (bare `python` resolves to a tool venv without deps).
- Config + secrets: `.env` (gitignored) with `ALPACA_*` paper keys +
  `TRADINGEXEC_*` settings; mirror keys in `.env.example`.
- Conventions: ruff (E/W/F/I/B/UP/C4/SIM), line length 100; every test carries
  a pytest-timeout deadline. See `docs/AGENT_ONBOARDING.md` — read it first.
- **Non-technical guide**: `docs/USER_GUIDE.md` — what the system does, how to
  read signals, safety rules, FAQ (no code knowledge needed).