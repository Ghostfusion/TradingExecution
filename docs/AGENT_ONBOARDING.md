# AGENT ONBOARDING — TradingExecution (read first!)

This file tells a fresh agent instance everything it needs to operate in this
repo without rediscovering the environment. Read it before running anything.

**Context:** `TradingExecution` is the EXECUTION layer in the research-to-
execution stack at `D:/Users/vince/PycharmProjects/TradingNew`:

- `TradingAgents/` — research layer (advisory: reports, `research_decision.json`)
- `trading_web/` — FastAPI + React web app
- `../Master_deign.md` — overall design contract
- `../EXECUTION_IMPLEMENTATION_PLAN.md` — this repo's implementation plan (Rev 3)
- **This repo** — the committed side: signal daemon (Phase A), later Alpaca
  paper orders (M1+).

Most conventions below are carried over from the TradingAgents onboarding
(`TradingAgents/docs/AGENT_ONBOARDING.md`) — they apply here too, adapted to
the execution-layer scope. Things specific to the research repo (moomoo/OpenD,
Finnhub, vendor chain, analyst tools) do **not** apply here.

---

## 0. WORKING AGREEMENT — read before EVERY task

1. **Design docs are the contract.** `../Master_deign.md` + `../EXECUTION_
   IMPLEMENTATION_PLAN.md` define the hard invariants: research is advisory /
   execution is committed; **fail closed** (any gate that cannot run → no
   signal or order); **no lookahead**; **deterministic** gates/sizes (pure
   functions — the daemon never parses prose numbers). Do not weaken them.
2. **Keep every doc true.** When behavior changes, update `README.md`,
   `CHANGELOG.md`, `docs/AGENT_ONBOARDING.md`, and the plan doc together.
   Never leave a doc stale against code.
3. **Always commit and push when done.** Conventional-Commits-style message;
   `git push origin main` (remote `Ghostfusion/TradingExecution`). Repo-local
   git identity is `vince <vince@localhost>` (global identity is missing —
   set repo-local if a fresh clone complains). Use **explicit `git add
   <paths>`** — user files (Master_deign.md, plans, session notes) may sit
   nearby untracked.
4. **Sibling contract.** The daemon's only input contract is
   `research_decision.json` emitted by TradingAgents (plan §3). If you change
   a phase-A capability that the web app can surface (future `GET
   /api/signals`, new channel, new mode), sync `trading_web` in the same task
   or note the follow-up — same rule as the research repo.
5. **Every test has a timer** (pytest-timeout). A hung vendor call must never
   block the session. Wrap shell test runs in `timeout` too.
6. **No secrets in commits.** `.env` holds real keys (Alpaca paper, notifier
   webhooks) and is gitignored; `.env.example` mirrors every key. NEVER print,
   commit, or paste them. Offline tests stay hermetic via the injected
   transport seam (fixture responses; **zero network in CI**).
7. **Deep web search before behavior-changing decisions.** Alpaca API
   semantics, order rules, and regulatory state change (e.g. PDT was retired
   and replaced by the Intraday Margin Rule on 2026-06-04) — verify against
   current docs, cite what you found, and say so when a search contradicts an
   assumption. Do not trust remembered-from-training broker/regulatory facts.
8. **Paper/signal only until explicitly opted in.** Phase A is mode `signal`
   (no orders, no `submit_order` path — a `NOT_IMPLEMENTED` guard raises).
   `paper` requires `--execute`; `live` requires two independent opt-ins
   (env + flag). Dry-run is the default; a second daemon instance must be
   refused (PID lockfile).

---

## 1. THE MOST COMMON MISTAKE — Python interpreter

Same as the research repo — two environments, NOT interchangeable:

| Command | Resolves to | pytest? | Use for |
| --- | --- | --- | --- |
| `python` (bare) | hermes agent venv | **NO** | nothing in this repo |
| `py -3.12` | Program Files Python312 | **YES** | EVERYTHING |

- `No module named pytest` / `pandas` ⇒ wrong interpreter. Use `py -3.12`.
- This repo's deps (`alpaca-py`, etc.) install into the `py3.12` env.

```bash
py -3.12 -c "import sys; print(sys.executable)"
py -3.12 -m pytest tests/ -q -p no:cacheprovider
py -3.12 -m ruff check signald/ tests/
```

## 2. Environment gotchas (Windows, inherited)

1. **CRLF files + byte-exact edits.** Most files here are CRLF. The `edit`
   tool can corrupt bytes — prefer `eval` with byte-exact replaces and
   `\r\n`-aware anchors; verify EOL (`sed … | cat -A`) before editing a file.
2. **Shell heredocs mangle code** (`\n`, em-dashes, arrows) — write scripts
   with the `write` tool, run, delete.
3. **ruff** is the linter/formatter: `py -3.12 -m ruff check` +
   `py -3.12 -m ruff format` (selectors E/W/F/I/B/UP/C4/SIM, line length 100).
   Keep the touched scope clean; don't regress the repo.
4. **`.env`** holds real keys — gitignored, never printed/committed.
   `TRADINGEXEC_*` + `ALPACA_*` overrides live there, not in code.

## 3. What this project does (execution layer)

Phase A `signald` pipeline (plan §4):

```
research_decision.json (from TradingAgents)
  → load + schema-validate + hash prechecks (fail closed on incomplete)
  → Signal Normalizer → SignalContract (agnostic: action/target_pct/stop/…)
  → mandate gates — ALL fail-closed (symbol/direction/size/exposure/cash
    reserve/tradeable/daily count/dup/data/price caliber/invalidation/
    staleness/approval)
  → signal envelope + Alpaca paper reference (close/vwap/spread, feed tag,
    expected-cost band)
  → channels: signals.jsonl + latest.json + terminal + notifier (webhook)
  → audit ledger (hash-chained) + kill switch + idempotency journal (decision_hash)
```

Key contracts / non-negotiables (plan §4.4–4.12):

- **Idempotency:** `decision_hash` dedupes signals today; at M1 a
  deterministic `client_order_id = f(decision_hash, leg)` — on timeout, query
  by `client_order_id` FIRST, never blindly resubmit (Alpaca doesn't
  guarantee duplicate rejection); "not found" ≠ safe resubmit. Broker is the
  source of truth: positions/order ledger re-synced each cycle.
- **Reality models (M1+, designed now):** Intraday Margin Rule (IML/IMD —
  PDT/$25k gone; **do not build day-trade counting**); Alpaca buying power
  (short-open value = MAX(limit, 3% above ask) × qty; open orders consume BP
  until executed/cancelled); paper pitfalls (no NBBO-qty check, random ~10%
  partial fills, marketable-only fills, IEX-only data for paper-only
  accounts, ~15-min SIP delay → stale-tag refs); settlement (cash T+2, margin
  immediate); extended hours (limit + TIF day/gtc only), IPO (limit pre-first-
  trade), sub-penny decimal rules (42210000 reject).
- **OrderStateManager (M1):** maintain order state via `TradingStream`
  `trade_updates` events (`new → accepted → partial_fill → fill`, plus
  rejected/canceled/expired/replaced/…), never `poll-until-done`.
- **Paper ≠ live:** paper fills are near-mid and ignore queue position/market
  impact (QSE phantom-profit audit). Publish an expected-cost band on every
  signal; realistic-fill check before any live path.

## 4. Project structure (current)

```
TradingExecution/
├── README.md  CHANGELOG.md  docs/AGENT_ONBOARDING.md
├── .github/                              # CI (placeholder)
├── .gitignore  .env.example (later)
├── signald/    # Phase A modules (landing): watch, normalizer, gates,
│               # envelope, channels, notifier, audit, journal, alpaca_ref
└── tests/      # hermetic; fixture artifacts, fake clock, injected transport
```

Plan and design live at the parent level: `../EXECUTION_IMPLEMENTATION_PLAN.md`,
`../Master_deign.md`.

## 5. Testing conventions

- **Hermetic by construction:** fixture `research_decision.json` files, fake
  clock (expiry/staleness), tmp watch dirs + tmp ledger. Alpaca
  account/clock/assets and webhook dispatch go through an **injectable HTTP
  seam** that returns fixture responses — the full pipeline runs with zero
  network in tests (same pattern as QSE's `IExecutionHandler` and the
  research repo's mocked vendor probes).
- **Every test has a timer** (pytest-timeout, e.g. 180 s).
- Coverage that matters, per gate: each gate fails closed on missing/
  unrunnable input; mandate expiry; hash-mismatch / schema-invalid reject;
  tradeable=false / market-closed; cash < reserve; stale-ref (15-min) tagged;
  normalizer direction→action incl. shorts-forbidden; idempotency replay ⇒
  one signal; restart recovery (journal replay, no dup/loss); tamper ⇒
  `verify` fails at first bad index; notifier down ⇒ signal still journaled;
  PID lockfile (second instance refused); old `effective_date` ⇒ blocked.

## 6. Changelog format (carried from TradingAgents)

Date-stamped entries, concise what/why, one bullet per logical change
(see `CHANGELOG.md` in this repo). Entry format: `2026-09-03 - <what> —
<why/effect>`.