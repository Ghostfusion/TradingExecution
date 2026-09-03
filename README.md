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

- **Phase A (design frozen, Rev 3; implementation pending):** `signald` daemon —
  watch `decisions/` → normalize to Signal Contract → fail-closed mandate gates →
  signal envelope (+Alpaca paper reference + expected-cost band) → channels
  (jsonl/terminal/notifier) → hash-chained audit ledger, kill switch,
  idempotency journal. **No order submission path exists.**
- Design contract: [`../EXECUTION_IMPLEMENTATION_PLAN.md`](../EXECUTION_IMPLEMENTATION_PLAN.md)
  (TradingNew level) built on [`../Master_deign.md`](../Master_deign.md).

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
├── signald/        (Phase A: watch, normalizer, gates, envelope, channels,
│                    audit, journal, notifier, Alpaca reference client)
├── tests/          (hermetic — injected transport seam, zero network)
└── .github/        (CI)
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