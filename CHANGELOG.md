# Changelog — TradingExecution

Format follows the TradingAgents repo (date-stamped entries, concise what/why).

## 2026-09-16 — documentation brought back in line with the code

An audit of every human-facing doc against `signald/` found six claims describing behaviour the daemon no
longer has, ten surfaces with no doc at all, and two stale counts. All corrected in place; no code changed.

- **The scan window is now documented** (README, USER_GUIDE, RUNBOOK's daily lifecycle, AGENT_ONBOARDING):
  the daemon only processes artifacts during the regular session (09:30-16:00 ET, 13:00 on half days, no
  weekends or holidays), evaluated in exchange time and cross-checked against the broker's `/clock`; a report
  that lands after the close waits for the next open, the heartbeat keeps ticking so the watchdog stays quiet,
  and `signald run --once` is the operator override. The old "market closed -> next-session signal" framing is
  now correctly described as reachable only through that override.
- **The mandate-promotion flow is now documented** (USER_GUIDE, RUNBOOK "Mandate change" procedure): an
  out-of-mandate artifact whose only binding block is the symbol check and whose rating is buy/overweight lands
  in `<data>/mandate_candidates.jsonl` and raises a Discord candidate card carrying
  `signald mandate-add <TICKER>`; `mandate-add` / `mandate-remove` re-sign and archive the mandate atomically, a
  changed mandate hot-reloads without a restart, refusals are journaled by mandate hash (so adding a symbol
  reopens that artifact exactly once) and emissions never carry one.
- **Allow-list wording corrected** (USER_GUIDE): the symbol check gates ENTRIES - a confirmed reduce/exit of a
  provably held name is exempt, unknown holdings keep the block (fail closed).
- **INTRADAY_ALGO_DESIGN gate table G1** now records the held-name exit exemption.
- **DESIGN.md**: the PDT rule is marked retired (2026-06-04, superseded by the Intraday Margin Rule) and
  explicitly not to be built; the gate verdict vocabulary is `PASS | DOWNGRADE | BLOCK` with `binding_gate`, and
  the entry rules are the shipped ones (never a market order).
- **Counts**: the suite is 1102 tests; `signald/risk/gate.py` has 16 named checks (the plan said 15).
- An extra drift found while editing: INTRADAY_ALGO_DESIGN §10 presented the soft-loss action as "halve size",
  which contradicts ladder rung 1 as shipped.

## 2026-09-15 (c) — the daemon scans only in the regular session, and the clock is UTC again

Owner request: *"make sure signald only scans during the stock market open."* The poll loop now honours
a **scan window**, and implementing it surfaced a timezone defect affecting every age computation.

- **Scan window** (`watch.WatchLoop.scan_window` + `run_forever`): with `scan_rth_only` (default ON) the
  daemon processes nothing outside the regular session - 09:30-16:00 ET on trading days, 13:00 on half
  days, no weekends or holidays (`marketdata.calendar`, which ships the 2026-2027 calendar and the DST
  rules). The window is an **exchange** fact in ET, so the host's zone never enters the decision: on this
  US-Central box the session is 08:30-15:00 local. With `scan_confirm_broker_clock` (default ON) the
  calendar is cross-checked against the broker's own clock, which knows about unscheduled halts and
  early closes the shipped calendar cannot; an unavailable clock means *do not scan* (fail closed). Each
  phase change is announced once, not every poll. `signald run --once` ignores the window on purpose -
  that is the operator override.
- **One loop, one behaviour**: `signald run` used to inline its own poll loop, so the window (or
  anything else added to `run_forever`) would have been inert; the CLI now calls
  `WatchLoop.run_forever`. The heartbeat is touched *before* the window check, so a closed market never
  looks like a dead daemon to `signald watchdog`.
- **The daemon clock is UTC again** (`config.now_utc`): it returned `datetime.now()` - host-local time
  under a UTC name - while `calendar`, `alpaca_ref._parse_ts` and `stores._parse_ts` all read a naive
  stamp as UTC. On this host that skewed every age by 5 h: a five-hour-old quote looked fresh, which
  disabled the `market.quote_age_s` staleness check. `gates._quote_age_s` normalises both sides to
  UTC-aware as well, so an aware injected clock can no longer raise `TypeError` (it did).
- **Config**: `scan_rth_only` / `scan_confirm_broker_clock`, both ON by default per the owner's
  "every built switch ships ON" decision (2026-09-13); the `run` banner prints the active policy.

Verified: **1102 hermetic tests green** (16 new: seven session cases, the broker-clock cross-check, the
disabled/trusted variants, loop-level skip and scan, the `--once` override, and the clock + quote-age
pins) and the live daemon prints `[scan] market post (… ET); not scanning` with a fresh heartbeat.

## 2026-09-15 (b) — operator-driven mandate widening: candidates, `mandate-add` / `mandate-remove`

Option A of the design question "how should a strongly-rated, not-held name reach execution?":
**the executor never widens its own mandate.** When a *valid* decision carries `buy`/`overweight` for
a symbol the mandate does not list *and the account provably does not hold*, the daemon queues a
candidate (JSONL row + one Discord card carrying the exact promotion command) instead of paging a bare
refusal. A human then runs `signald mandate-add <TICKER>`.

- **`signald mandate-add SYMBOL` / `signald mandate-remove SYMBOL`** (`signald/cli.py`): both re-sign
  through `write_mandate` (never hand-edit - the loader rejects an unsigned file), audit a
  `mandate_added`/`mandate_removed` row naming the operator, and write provenance into the archive.
  `add` is a no-op when the symbol is already allowed (no hash churn); `remove` refuses the last
  symbol, and refuses a symbol you hold - or one whose holdings cannot be verified - unless `--force`,
  because removal stops new entries and the gate can only exempt an exit it can prove.
- **Candidate queue** (`signald/stores.py::CandidateStore`, `processor._offer_candidate`): append-only
  `<data>/mandate_candidates.jsonl`, idempotent per (ticker, decision_hash). Unknown holdings are not
  proof, so they keep the plain refusal (fail closed).
- **Per-symbol holdings** (`alpaca_ref`): `get_all_positions()` was fetched and then collapsed into a
  single `positions_value`; `RefData.held_symbols` now carries the symbol set (or `None` when unknown).
  Deliberately absent from `RefData.as_dict()` - the envelope's `ref` block is a price record consumed
  by the web dashboard and the v2 schema, so account composition stays out of it.
- **The allow-list gates entries, not exits** (`risk/gate.py::_reduces_a_held_name`): a reduce/exit for
  a symbol the account provably holds is no longer blocked by the symbol check. Removing a held name
  used to strand its research exit path; now only a confirmed *open* short intent is refused, and
  unknown holdings keep the block.
- **Refusals expire with the mandate** (`stores.Journal`): only refusal rows carry `mandate_hash`, so
  `mandate-add` re-opens a refused artifact exactly once. Emission rows carry none - a mandate edit
  must never replay a signal. This replaces the hand-pruning of `journal.jsonl` that the entry below
  had to document.
- **The daemon reloads the mandate** (`watch.run_once` -> `processor.refresh_mandate`): the mandate was
  read once at startup, so an operator change needed a restart. A changed file is now picked up on the
  next poll and audited as `mandate_reloaded`; an unparseable edit keeps the loaded mandate and is
  audited/paged once per distinct reason (fail closed, no per-poll spam).
- **Atomic mandate write** (`mandate.write_mandate`): tmp + `os.replace` (the `kill_switch` idiom), with
  the archive row carrying `actor/operator/reason/ticker`. The loader fails closed, so a torn file -
  e.g. two batch workers writing at once - would have left the daemon unable to start.

Verified: **1086 hermetic tests green** (20 new) and a live sandbox run (isolated mandate/data/env, no
notifier, real paper reference): the artifact was refused once, queued as a candidate, promoted with
`signald mandate-add NFLX`, hot-reloaded (`mandate_reloaded`) and emitted
(`[emitted] NFLX BUY target_pct=0.03 signal_id=sg-…-001`) with no restart. Operator note: run the verbs
with the same `--data` as the daemon so their audit rows land in the same ledger.

## 2026-09-15 — first live daemon run: a refusal is recorded once, the ledger stops chaining poll noise

The reports-tree daemon ran for real against TradingAgents' live `reports/` tree
(`signald run --watch …/reports --data ./signals --execute`, `mode=paper`), fed by the research
layer's first emitted artifact. Two defects surfaced in the first 44 seconds and are fixed here;
`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` (the same paper account the research repo reads) are now in
`.env`, which the daemon requires before it will start.

- **A refused artifact re-fired on every poll** (`signald/processor.py`). A gate `BLOCK` returned
  without touching the journal, and `watch.discover()` re-discovers every artifact still sitting in
  the tree, so one ineligible decision produced a `rejected` audit row **and a Discord error card
  every 10 s** — measured: 4 rows and 4 cards in 44 s for `NFLX` (outside the mandate allow-list).
  A refusal is a durable verdict on that artifact, so the blocked path now calls
  `journal.mark_processed(...)` exactly like an emission: one row, one page, then silence.
  Consequence: later polls do not re-evaluate a refusal — widening the mandate will not revive the
  same artifact. Re-evaluation needs a new `decision_hash` (re-run the symbol) or pruning the
  journal row (`<data>/audit/journal.jsonl`; the chained audit ledger is never touched).
- **The duplicate skip grew the audit ledger without bound.** `process()` appended a
  `skipped_duplicate` row on every poll cycle for every already-handled artifact (~8.6k rows/day
  each, into a SHA-256 chained append-only ledger that cannot be pruned). The skip is now silent:
  the ledger records decisions, not poll cycles.
- **`signald verify` crashed without `--audit`** (`Path(None)`), on the one command an operator
  needs to check the daemon's ledger. It now resolves the configured ledger the way `status` does,
  and gained `--env`/`--data` (`--audit` stays the explicit override).

Verified: **1066 hermetic tests green** (two new processor tests, one new CLI test), the ledger
chains (`signald verify --data ./signals` → `OK — 5 rows chained`), and a restarted daemon shows
`[blocked] NFLX …` once, then silent `[skipped_duplicate]` cycles with a live heartbeat.

## 2026-09-13 (b) — control-surface launchers + hermetic test environment

`signald api` and `signald mcp` now exist (`signald/control.py`): they build the signed control
API / MCP surface with the seams this repo can honestly serve — a local-state snapshot (signals,
pending orders, kill switch, config hash), `halt` (the same sentinel + episode latch + audit row
as `signald halt`), and the MCP proposal/approval stores under `audit/`. Broker-backed reads
(`account`/`positions`/`risk`/`sleeves`) answer `null`, and the order-path effects
(`submit`/`cancel`/`ceiling`, MCP `submit_order`/`set_sleeve_allocation`) stay unwired, so those
routes keep answering `503 api_disabled` / `*_unavailable` instead of half-wiring a flag. The API
refuses to start without `TRADINGEXEC_API_KEY_ID` + `TRADINGEXEC_API_SIGNING_SECRET` (mapped to
the `admin` role), and both launchers refuse a non-loopback bind before a socket exists.
`cmd_halt` now shares `control.halt_now`, so CLI/API/MCP halts leave identical trail.
Fixing the launcher exposed a config defect: the env alias table
(`api_key_id` → `alpaca_key`) was applied to every prefix, so `TRADINGEXEC_API_KEY_ID` was
silently read as the *Alpaca* key and the control API could never see its own key id. The
aliases now apply to the `ALPACA_` prefix only (their purpose), and `tests/test_config.py` pins
both sides.
18 new tests (`tests/test_control.py`) drive both servers through their own wiring; no socket is
opened.

Test environment: the 12 failures that predated the switch flip are fixed at the root.
`tests/conftest.py` strips ambient `TRADINGEXEC_*`/`ALPACA_*` from the process environment for
every test — `load_config` prefers the shell over `--env`, so an exported
`TRADINGEXEC_WATCH_DIR`/`TRADINGEXEC_NOTIFIER_URL` was silently redirecting the probe and
notify-test CLI tests onto the operator's real tree. `tests/test_inbox.py` and
`tests/test_order_manager.py` dated their artifacts and row expectations from a fixed past day
and dead-lettered (or mismatched) once the wall clock moved past it; both now derive from the
injected clock. **1063 hermetic tests green, ruff clean.**

## 2026-09-13 — owner decision: every built switch ships ON

`Config` defaults flipped: `mode` `signal` → `paper`, `intraday_enabled` `False` → `True`,
`api_enabled` `False` → `True`, `mcp_enabled` `False` → `True`. The four switches are also set
explicitly in `.env` (gitignored) and `.env.example` documents the new defaults.

Nothing about the send gates moved: the order path is *reachable* by default, but the session
engine still refuses to arm without `execute=True` (CLI `--execute`), `live` still needs its
second acknowledgement, the intraday router still needs the caller's `enabled=True` on top of
the config flag, and `api_enabled` / `mcp_enabled` gate the control surfaces on the loopback
bind — nothing listens until `signald api` / `signald mcp` runs (added the same day, below).
Live broker/market-data adapters remain unwritten. `cli.py`'s banner now prints the real
`cfg.mode` (it printed the word `signal` for any non-dry-run process). Docs updated to match:
`README.md`, `docs/USER_GUIDE.md` (§2, §8), `docs/AGENT_ONBOARDING.md`,
`docs/INTRADAY_ALGO_IMPLEMENTATION.md` (§3 table, §8 phase gate),
`docs/INTRADAY_ALGO_DESIGN.md`; `tests/test_config.py` pins the new defaults and the
signal-mode property.

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