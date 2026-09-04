# Changelog — TradingExecution

Format follows the TradingAgents repo (date-stamped entries, concise what/why).

## 2026-09-03

- **Reports-tree watch + newest-per-symbol + PM decision persistence.**
  - `signald/watch.py` `discover()`: watches the TradingAgents `reports/` tree
    recursively for `research_decision.json` (run_card.json noise ignored),
    keeps only the NEWEST decision per ticker by mtime (`latest_only`,
    default on) — a symbol's older runs never emit. `.env` now points
    `TRADINGEXEC_WATCH_DIR` at the reports tree.
  - Research-side: `pm_decision` structured PortfolioDecision now persisted
    into graph state + returned by the PM node (captured after the guardrail
    hook), so `research_decision.json` carries real rating/data_quality/
    guardrail_reason on new runs. Legacy artifacts (pre-fix, rating null)
    correctly fail closed as invalid rather than being guessed. 4 watch
    discovery tests; 68 hermetic total; ruff clean.
- **Non-technical user guide** — `docs/USER_GUIDE.md`: plain-language
  explanation of what the system does (signals only, no orders), the safety
  mandate rules, how to read signal notifications (PASS/DOWNGRADE/BLOCK,
  cost band, stale-data flags), the web dashboard panel, what is NOT built
  yet, the kill switch, and an FAQ. README links it.
- **Discord notifier live.** Signals render as readable Discord content+embeds
  (action emoji, ticker, verdict, ref price, expected-cost band, stop/target)
  instead of raw JSON; `discordapp.com` legacy domain matched; browser-like
  User-Agent sent so Discord's Cloudflare edge accepts the POST (403/1010
  without it). Live-verified with `signald notify-test` + a real signal
  dispatch. Webhook URL lives in gitignored `.env`
  (`TRADINGEXEC_NOTIFIER_URL`); `.env.example` documents setup. 64 hermetic
  tests; ruff clean.
- **Watchdog + notifier wiring + web signal feed.**
  - `signald/watchdog.py` + CLI `watchdog` — heartbeat freshness check,
    dispatches `heartbeat_loss` notifier event + non-zero exit when stale
    (force-flatten arrives with M1).
  - CLI `notify-test` — sends a test webhook to the configured notifier.
  - Processor now dispatches notifier `error` events on invalid/blocked/
    reference-unavailable outcomes (best-effort, journal-first).
  - Missing `TradingExecution` signals dir + `GET /api/signals` (read-only)
    in `trading_web` (capability `list_signals`, Dashboard signals panel,
    `TRADINGEXEC_REPO` env, hermetic route test) — sibling-sync rule.
  - 61 hermetic tests; ruff clean.
- **Live Alpaca paper verification (real paper keys).** `TRADINGAGENTS_ALPACA_*`
  env names now load (prefix + `api_key_id`/`api_secret` aliases); real
  alpaca-py quote parsing fixed (flat `bid_price`/`ask_price` — markets closed
  now, the snapshot legitimately returns last=None/spread=None/stale with
  $100k cash/$400k buying power/AVGO tradable). End-to-end `signald run
  --execute` emitted a real signal from the live paper reference (AVGO REDUCE,
  verdict DOWNGRADE, config-hash + 1-row audit chain verified). Tests
  environment-isolated from ambient env (monkeypatch.delenv).
- **Phase A implemented — `signald` signal daemon (no order path).**
  - `config.py` env parsing (`.env` + `TRADINGEXEC_*`/`ALPACA_*`, alias
    mapping, config hash excludes secrets), `schema.py` (ResearchDecision
    validation + hash-pinning + SignalContract normalizer), `mandate.py`
    (hash-pinned mandate, expiry, re-sign + archive).
  - `gates.py`: all fail-closed gates (symbol/direction/size/exposure/cash
    reserve/tradeable/daily cap/cooldown/data quality/price caliber/
    invalidation/staleness/approval) + reference-completeness fail-closed.
  - `alpaca_ref.py` injectable seam (hermetic zero-network testing); reference
    reads only — `NOT_IMPLEMENTED` rule: no order submission in Phase A.
  - `stores.py`: signals.jsonl + latest.json, idempotency journal,
    SHA-256-chained audit ledger + `verify`. `notifier.py` webhook events
    (signal/error/kill_switch) journal-first. `kill_switch.py` sentinel +
    persisted HALT episode latch. `daemon.py` PID lockfile + heartbeat.
    `watch.py` poll loop. `cli.py`: run/verify/status/sample/approve/
    init-mandate; dry-run default; `--execute` opt-in.
  - 53 hermetic tests (gates/idempotency/audit-tamper/restart-recovery/
    notifier-down/kill-switch/CLI/config), ruff clean.
  - **Research-side emitter shipped in TradingAgents `48912e7`**:
    `write_research_decision` emits the hash-pinned `research_decision.json`
    beside every report tree (deterministic contract; nulls for
    unproducible fields; advisory).
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