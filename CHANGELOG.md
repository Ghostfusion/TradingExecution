# Changelog — TradingExecution

Format follows the TradingAgents repo (date-stamped entries, concise what/why).

## 2026-09-12 (b) — two-sleeve intraday execution, phases P0–P6 implemented

The design/plan entries below became code. 36 new modules, 34 new test files,
**1044 hermetic tests green, ruff clean**; the order path is unreachable unless
`mode` is `paper`/`live` **and** the caller passes `execute=True`, and every
submission carries a `GateDecision`.

**P0 — boundary & contracts.** `contracts/research_decision.v1.schema.json`,
`signal.v2.schema.json`, `order_intent.v1.schema.json`; `signald/contracts.py`
(semver negotiation, closed enums, expiry, artifact-hash pinning, dead-letter with
a `.reason.json` sidecar); `signald/inbox.py` (effectively-once ingest: the
admission row is written *before* the side effect, a replay is `deduped`, a
non-permissible signal is quarantined); config v2 (~90 `TRADINGEXEC_*` keys,
fail-closed validation at construction) and the **removal of the
`TRADINGAGENTS_ALPACA_*` env alias** (boundary rule design §2.2 rule 3);
`.github/workflows/ci.yml` with a `test` + `independence` job.

**P1 — sleeves, gate, budgets (signals only).** `sleeves/{router,swing,intraday,budgets}`;
`risk/state.py` (the typed gate inputs), `risk/tail.py` (three ES estimators, take
the worst), `risk/voltarget.py` (EWMA scalar with warm-up and band), `risk/ladder.py`
(the four rungs), `risk/sizing.py` (the single sizer: caps → vol → Kelly →
liquidity → notional → floor), and `risk/gate.py` — 16 named checks with fixed
precedence, `binding_gate`, and `REDUCE` allowed only for `vol_regime`,
`house_cvar`, `concentration`, `liquidity`, `sleeve_capital`. The Phase-A gates are
**folded in**: `signald/gates.py` is now a signal-stage adapter over the one
verdict producer (its 17 tests unchanged). The envelope gains `sleeve`,
`opportunity_score`, `trade_permission`, `binding_gate`, `permission_reason*`,
`idempotency_key`, `stop_kind` — additive only.

**P2 — paper order path.** `order/prices.py` (the tick rule: one owner for
rounding *and* validating), `order/policy.py` (slice count, marketable vs resting
limits, never a market order), `order/guard.py` (12 pre-trade checks incl.
precision, bracket legality, duplicate `client_order_id`, self-cross),
`order/manager.py` (own-before-write pending store, query-before-retry, never a
blind resend), `order/state.py` (trade-updates machine: monotone, anomalies
recorded, nothing lost), `order/stops.py` (synthetic stop-limit + cluster
clearance), `order/halts.py` (LULD state machine + reopen cooldown), `order/flatten.py`
(flat verification is the only thing that may declare the book flat), `reconcile.py`
(fills-not-targets; a contradiction halts), `engine.py` (the session composition).

**P3 — intraday sleeve.** `marketdata/{calendar,clock,service,daytype}` (code-shipped
sessions/holidays/computus, NTP offset vs the 50 ms bound, bars/quotes with
provenance + freshness + quarantine, ADX/VWAP/RVOL/gap classifier) and
`signals/{setups,costgate}` (the five setups, one stop arithmetic, ATR-scaled
expectation vs round-trip cost with the sqrt impact law).

**P4 — validation.** `validation/stats.py` (DSR, PBO/CSCV, MinBTL, stationary
bootstrap, paired comparison, Newey-West), `validation/harness.py` (a replay that
calls the *same* gate/sizer; no lookahead — a decision at bar i sees bars 0..i and
fills at bar i+1's open), `lineage.py` (trial registry with N first-class,
scorecard with None+reason instead of zeros, comparator that refuses to declare a
winner on a short sample, diversification stats, allocation bands).

**P5/P6 — guarded live + review.** `api/{signing,server}.py` (HMAC canonical
string, single-use nonce, replay window, role scopes, deny-by-default, audited),
`mcp/{tools,server}.py` (12-tool allow-list, descriptions hash-pinned, no
LLM-authored number, approval bound to the proposal hash, single-use),
`promotion.py` (the §11.4 checklist as machine-checked items, two opt-ins),
`docs/RUNBOOK.md` (daily lifecycle, incident table, kill/re-arm with staged size
restore, RTO/RPO), `cli.py` gains `probe`, `simulate`, `halt`/`--resume` (re-arm
requires a post-mortem reference), `scorecard --review` (the pre-registered
kill/shrink verdict, computed from the journal: a kill needs ≥100 trades and an
adequate sample, an interval that still touches zero shrinks to 10%, and an
unavailable input can never cause a kill).

**Two boundary defects found by writing the consumer's tests** (both in the
research-side plan `TradingAgents/docs/execution_v1_emitter_plan.md`):
1. the research emitter's advisory `binding_gate` collides with the gate-owned
   field name (`halt` matches by accident; `book_drawdown` would have
   dead-lettered the artifact) → on ingest it is display data (ignored) and the
   research side renames it `binding_constraint`;
2. envelope timestamps must carry an explicit UTC offset — a naive stamp is
   rejected (`naive_timestamp`) rather than silently shifted by the host offset,
   and `_as_aware` now converts a naive *clock* as local time.

**Two default-calibration mismatches found by the paper drill** (kept as shipped;
the owner decides): (a) the 1% house ES budget is below the stress-grid floor
(25%-of-sleeve cluster × 10% gap = 0.75% before any vol term), so
`es_budget_*` must rise or the cluster cap must fall before the intraday sleeve
can trade; (b) 0.25% risk at a 1.75×ATR stop implies a ~14% position, above the
5% single-name and 25%-of-sleeve cluster caps, so a trade is reduced or blocked
before it reaches its configured risk.

**Mutation proofs** (each restored byte-identical; earlier phases' proofs are in
their own suites): mandate check disabled → 5 gate tests fail; precedence reversed
→ 2 fail; dedupe table off → 4 fail; `execute` opt-in ignored → 1 fail;
`verify_flat` always flat → 2 fail; `market` added to the allowed order types → 2
fail; sleeve notional cap ignored → 6 fail.

**Not in this change** (P5 deployment work, no code): the live broker/market-data
adapters (`alpaca_ref` is still read-only; `MarketDataService`/`OrderManager` take
injected transports), the `session` CLI command that would wire them, and the
`TradeUpdate` websocket. Nothing was enabled: default mode is `signal`.

## 2026-09-12 (a) — design + plan (docs only)

- **Two-sleeve algo intraday design + implementation plan (docs only — no code, nothing enabled).**
  - `docs/INTRADAY_ALGO_DESIGN.md`: two sleeves (value-dip swing fed by research, intraday algo self-signalled)
    under **one house risk gate** with separate capital ceilings (70/30 default) so they never compete for
    capital blindly; `opportunity_score` (producer) vs `trade_permission`/`binding_gate` (gate) so "no BUY
    because unattractive" and "no BUY because risk forbids" are never conflated; total research↔execution
    separation (no shared code/runtime/keys; artifact drop + signed HTTPS API + MCP); honest evidence section
    (retail day-trader base rates, contested ORB replication, cost floors) and a pre-registered kill criterion.
  - `docs/INTRADAY_ALGO_IMPLEMENTATION.md`: contracts v2 (additive envelope + field mapping from today's
    `schema.py`), ~60 new `TRADINGEXEC_*` keys with defaults, 27-component catalogue with fail-closed
    behaviour, the 15-check gate and its precedence, one sizer, formulas (EWMA vol target, ES/CVaR, fixed-risk
    sizing, fractional Kelly, sqrt impact, risk of ruin, DSR/PBO/MinBTL), control-API signing recipe, MCP tool
    table with approval binding, phases P0–P6 with exit criteria, hermetic test taxonomy + 12 drills, alert
    set, scorecard spec and the 2026-01-02→2026-09-11 fair-comparison protocol.
  - README links both. No module, config, or test file was touched; the order path remains absent.

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