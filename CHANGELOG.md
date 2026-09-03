# Changelog — TradingExecution

Format follows the TradingAgents repo (date-stamped entries, concise what/why).

## 2026-09-03

- **Repo scaffold** — `TradingExecution/` created at TradingNew level, git repo
  initialized (`main`), README + `.gitignore` + `.github/` committed and pushed
  to `https://github.com/Ghostfusion/TradingExecution`. Commit `6ccc4cc`.
- **Design research + implementation plan** — studied alpaca-py, QuantConnect
  LEAN + Lean.Brokerages.Alpaca, QSE, Alpaca official docs (Trading API,
  Orders, Paper Trading, WebSocket Streaming, Intraday Margin Rule), and
  community execution bots. Plan Rev 3 written at
  `../EXECUTION_IMPLEMENTATION_PLAN.md` (TradingNew level): **signal-first
  daemon** (Phase A: `signald` emits signals, no orders), Alpaca-paper-only
  reference data, fail-closed mandate gates, Signal Contract normalization,
  hash-chained audit + kill switch + idempotency journal, expected-cost bands
  (QSE phantom-profit lesson), OrderStateManager via `TradingStream`,
  **Intraday Margin Rule replaces the retired PDT/$25k rule (FINRA 4210,
  2026-06-04)**, buying-power/short-value rules, paper-pitfall guards
  (IEX-only, ~15-min SIP delay, NBBO-qty unchecked).
- **Onboarding docs** — revised `README.md`; added `CHANGELOG.md` (this file);
  added `docs/AGENT_ONBOARDING.md` carrying the applicable working agreement,
  interpreter/Windows gotchas, testing and changelog conventions from the
  TradingAgents research repo.