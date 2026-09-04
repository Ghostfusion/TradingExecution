# TradingExecution — User Guide (for non-technical users)

*What this system does, what it sends you, and what it does NOT do — in plain
language. No code knowledge needed.*

---

## 1. What is this?

**TradingExecution** is the "action side" of your trading stack. Your other
project, **TradingAgents**, researches stocks (analyses, debates, and writes
reports). TradingExecution takes those research conclusions, checks them
against the safety rules **you** set, and — when everything passes — sends you
a **signal** telling you what the research suggests.

Think of it as a careful assistant: it never acts on its own, it only *tells
you* what it thinks should happen, and only after checking many safety boxes.

## 2. What it does today (and what it does NOT do)

**Today (Phase A):**

- Watches for new research decisions from TradingAgents
- Checks every decision against your safety rules
- Sends you a clear **notification** (Discord) with the suggested action
- Keeps a tamper-proof log of everything it decided and why

**It does NOT (yet):**

- ❌ Place any buy/sell orders anywhere — **no trading happens automatically**
- ❌ Touch real money
- ❌ Connect to any broker to execute trades

A **signal is advice only**. You decide whether to act on it.

## 3. How it works — the flow

```
1. TradingAgents finishes a stock report (e.g. AVGO)
        │
        ▼
2. A short machine-readable "decision file" is written
        │
        ▼
3. TradingExecution (the daemon) picks it up
        │
        ▼
4. It checks the decision against YOUR safety rules
        │        ├─ all rules pass → ✅ signal is created
        │        └─ a rule fails → ❌ no signal (the reason is noted)
        ▼
5. You get a Discord notification with the signal details
```

The daemon checks for new decisions regularly (about every 10 seconds) and
runs in the background like a small always-on helper.

## 4. Your safety rules (the "mandate")

You control a set of rules that every signal must pass. Think of them as the
boundaries of what you allow the system to suggest:

| Rule | What it means |
|---|---|
| **Allowed symbols** | Only these stocks (e.g. SPY, AVGO, MSFT…) may produce signals |
| **No shorting** | The system never suggests betting against a stock (unless you change this) |
| **Order size cap** | A suggested trade larger than your cap gets flagged/downscaled |
| **Exposure cap** | Total suggested positions across the account stay under your limit |
| **Cash reserve** | If your account cash falls below your minimum, suggestions stop |
| **Tradable check** | Only real, currently-tradeable stocks; market-closed signals are tagged as "next session" |
| **Daily limit** | A maximum number of signals per day (anti-spam) |
| **Fresh data** | Signals based on stale/old data are rejected |
| **No duplicate** | The same decision is never sent twice |
| **Expiry** | The rules expire on a set date and must be renewed |

> **The golden rule:** if any check cannot be performed, the signal is **not
> sent** — the system would rather stay silent than guess.

## 5. What a signal tells you

You'll get a notification like this:

> 🔻 **Signal sg-…001 AVGO REDUCE** (verdict DOWNGRADE) | ref 355.6 | cost 3-8bps
> - Target %: 0.0
> - Stop: 429.0
> - Expiry: 2027-01-01

Translation:

- **Action**: what the research suggests — 🟢 BUY / 🔻 REDUCE / ⏹ EXIT / ⏸ HOLD / 🔔 NONE
- **Verdict**: `PASS` (all rules ok), `DOWNGRADE` (sent, but with a warning),
  or `BLOCK` (not sent — a rule failed)
- **ref price**: the latest market price used for background
- **cost band**: estimated trading cost range (in basis points) — the system
  is honest that trades are not free
- **Stop / Take-profit**: suggested risk levels from the research

**Common downgrade reasons you may see:**

- "market closed" → the signal is for the **next trading session**
- "data quality partial" → some data was missing; treat the signal as weaker
- "cooldown" → the same suggestion was already sent recently

## 6. Where you see signals

- **Discord** — instant notifications (what you're set up with now)
- **Web dashboard** — the "Execution signals" panel in your trading web app
  shows recent signals (read-only)
- **Files** — every signal is also saved internally so nothing is ever lost

## 7. Safety & honesty features

- **Fail-closed**: if it can't verify something, it says nothing rather than
  guessing.
- **Tamper-proof log**: every decision is recorded in a linked chain — if
  anyone edits the log, the system detects it (`verify` check).
- **Kill switch**: a single file can instantly stop all signals if you ever
  need to halt everything.
- **No fabricated numbers**: every figure in a signal comes from real,
  computed data — never invented by an AI.
- **Market data honesty**: prices are labelled with their source; stale quotes
  are flagged, not hidden (learned from a real bug where one data feed
  disagreed with another).

## 8. Things that are NOT built yet (roadmap)

| Coming later | What it means |
|---|---|
| **Paper trading** | Actually placing simulated orders at a broker (no real money) |
| **Live trading** | Real orders — requires you to explicitly opt in twice |
| **Auto-exit (sweeper)** | Automatically closing positions on a halt — not yet |
| **More brokers** | Only Alpaca planning so far; multi-broker later |

Until you explicitly say otherwise, **this system only ever sends signals. It
never trades.**

## 9. How to run it (the one command you may ever need)

The daemon runs in the background and needs no attention. To start it and
leave it running:

```
py -3.12 -m signald run --execute
```

(Start it from the TradingExecution folder. Details live in the technical
docs — as a non-technical user you generally don't need to touch it.)

## 10. FAQ

**Q: Will it trade for me?**
No. Phase A sends signals (advice) only. Order placement comes later and only
with your explicit permission.

**Q: Why didn't I get a signal for a stock?**
Most likely a safety rule: the stock isn't in your allowed list, the account
cash is below your reserve, the data was stale, or the market is closed
(in which case it would be tagged "next session" instead).

**Q: What if the market is closed?**
Signals are still produced but tagged as next-session — you see the price at
close and the suggestion applies to the next open.

**Q: Is this financial advice?**
No. It's an automated summary of research against your rules. Decisions to
trade are yours alone.

**Q: How do I stop it?**
Drop a file named `kill_switch` at the configured path — everything halts
instantly. (Or stop the running process; the technical guide has details.)

**Q: Where is my data stored?**
Local folders inside the project (`decisions/`, `signals/`, `audit/`). Nothing
is sent anywhere except the notifications you asked for.

---

*See `README.md` (technical) and `docs/AGENT_ONBOARDING.md` (for agents) for
the engineering details. This guide is for people who only want to know what
the system does and how to read its output.*