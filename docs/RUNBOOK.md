# Runbook — `signald` (two-sleeve intraday execution)

Operational procedures for the signal daemon and, once promoted, the bounded order path.
Plan references: §11.1 lifecycle, §11.2 incidents, §11.3 kill switch/re-arm, §11.4 promotion,
§9.3 drills, §9.4 alerts.  Time is ET (RTH).  Commands are pasteable from the repo root with
`py -3.12`.  Lines marked **(planned)** are not yet exposed by `signald/cli.py`.

---

## Daily lifecycle

| Time | Action | Command |
|---|---|---|
| 08:00 | pre-open: session calendar, NTP offset, data connectivity, buying power, IML/IMD, mandate validity, kill switch armed, heartbeat | `py -3.12 -m signald status` |
| 09:00 | universe scan + RVOL baseline; swing sleeve evaluates new research artifacts | `py -3.12 -m signald run --once --watch ./decisions --data ./signals` |
| 09:35 | day-type classification; intraday eligibility set (RVOL/rank/liquidity/regime) | `py -3.12 -m signald run --once --watch ./decisions --data ./signals` |
| 09:35–11:00 | intraday entry window; every order passes gate → sizer → OrderGuard | `py -3.12 -m signald run --execute --watch ./decisions --data ./signals` |
| 11:00–15:45 | manage only: stops, targets, time stops; **no new intraday entries** | `py -3.12 -m signald run --execute` |
| 15:45–15:55 | flatten intraday; verify flat against the broker; closing-auction preference where eligible | `py -3.12 -m signald flatten` **(planned: the machine is `signald/order/flatten.py`; no CLI command yet)** |
| 16:15 | post-close: reconcile fills vs positions vs journal; compute the day's scorecard; write the audit summary | `py -3.12 -m signald scorecard --trades trade_rows.jsonl` |
| 17:00 | report: sleeve PnL, slip vs model, gate histogram, data-quality summary | `py -3.12 -m signald scorecard --trades trade_rows.jsonl --review review.json` |

**Scan window.** The daemon only *scans* during the regular session — 09:30–16:00 ET, 13:00 on
half days, no weekends/holidays — evaluated on the exchange clock and cross-checked against the
broker's `/clock` (`TRADINGEXEC_SCAN_RTH_ONLY` / `TRADINGEXEC_SCAN_CONFIRM_BROKER_CLOCK`, both
`true`; an unavailable clock means *do not scan*). Outside it the loop is idle by design and prints
one `[scan] market post (… ET); not scanning` line per phase. The `--once` rows above (09:00,
09:35) are the operator override and bypass the window; the heartbeat ticks either way, so a
closed market never looks like a dead daemon.

Every loop writes a heartbeat; a stale heartbeat pages via the watchdog:
`py -3.12 -m signald watchdog`.

### Supervision — the two pieces that make that page real

Both pieces below are **scheduled** — the watchdog is not something you run by hand, and a daemon that dies
is paged, not silent (2026-09-16: the process was gone for a whole regular session and the operator learned
about it from the log, not a page — that incident is what the watchdog task was built for).

**Detection is automatic. Recovery is not.** Measured on the 2026-09-18 death, and worth reading before
trusting the pair:

- The daemon died **15:15 CT** (`^C` in `signals/signald_daemon.log`; heartbeat mtime 15:15:02).
  `Signald_Daemon`'s `Last Result` is `-1073741510` (`0xC000013A`, `STATUS_CONTROL_C_EXIT`) — a deliberate
  `schtasks /end` or a console Ctrl+C, not a crash.
- `RestartOnFailure` **did not fire**, and cannot: it retries a *failed* run, and a deliberate end is not a
  failure. So ending the task is a **one-way door** until the next slot.
- `Signald_Watchdog` **did** fire, at **15:30** (its last slot that day), `Last Result: 1` — the
  `heartbeat_loss` page. Detection worked.
- Nothing restarted the daemon. It stayed down **52.3 h** and did not return until the next weekday slot.
- **The window is the exposure.** The watchdog runs weekdays 08:00–15:30 only, so a death late in the session
  is paged **exactly once** and then silent across the weekend. Friday 15:15 is the worst case: one page, then
  nothing until Monday 08:00.

So: use `schtasks /end` only when you intend to leave it down, and prefer `--once` for a manual check.

- **The daemon runs under Task Scheduler, never under a terminal.** Task `Signald_Daemon` runs
  `run_daemon.cmd` (repo root) at logon and daily 08:00 CT on weekdays, with `RestartOnFailure` (every minute,
  up to 10 tries), no execution-time limit and no stop-on-battery or stop-on-idle. A terminal-launched daemon
  dies with its window — that is precisely what ended the 2026-09-16 session — so launch it by hand only as
  `run --once`.

```powershell
schtasks /run   /tn Signald_Daemon                        # start it now
schtasks /end   /tn Signald_Daemon                        # stop it (terminates the process tree)
schtasks /query /tn Signald_Daemon /v /fo LIST            # status, last result, next run
```

  Its stdout/stderr append to `signals/signald_daemon.log` (append + unbuffered, so it is readable while the
  daemon runs; rotate it by hand if it ever grows — the steady state is a handful of lines a day, not the ~26k
  the pre-2026-09-16 poll loop wrote). The task is interactive-token and least-privilege, so it needs the
  operator logged on; a machine-wide service (NSSM, per INTRADAY_ALGO_DESIGN) is the move if the daemon must
  survive a logoff.
- **A scheduled watchdog.** Task Scheduler task `Signald_Watchdog`: weekdays, starting 08:00 CT, every 5
  minutes for 7 h 30 m. It must run with the repo root as its working directory (so `.env` supplies
  `TRADINGEXEC_NOTIFIER_URL`) *and* point at the live heartbeat, which lives under the data dir the daemon was
  given (`--data ./signals` → `signals/audit/heartbeat`), not at the config default `audit/heartbeat`:

```powershell
py -3.12 -m signald watchdog --heartbeat .\signals\audit\heartbeat
```

  A 5-minute cadence detects a loss inside ~7 minutes (`DEFAULT_MAX_AGE_S` is 120 s). The heartbeat is touched
  every poll *before* the window check, so a closed market is fresh and only a stopped or wedged daemon pages
  (`heartbeat_loss` to the notifier). **A deliberate stop must disable the task** —
  `schtasks /change /tn Signald_Watchdog /disable` — or it pages every 5 minutes until you do.
- **After a host reboot**, logging on starts the daemon (logon trigger) and the watchdog resumes on its next
  slot — `StartWhenAvailable` lets a slot missed while the machine was off run as soon as it is back. A reboot
  with nobody logging on leaves the daemon down; that is the interactive-token tradeoff above, stated here so
  it is not a surprise. Confirm with `signald status`.

---

## Control surfaces (`signald api`, `signald mcp`)

Both bind loopback only and refuse any other host before a socket exists. Neither is
started by the daemon: a flag is a permission, not a listener.

```bash
py -3.12 -m signald api --bind 127.0.0.1:8787   # needs TRADINGEXEC_API_KEY_ID + API_SIGNING_SECRET
py -3.12 -m signald mcp --bind 127.0.0.1        # JSON-RPC/HTTP; toolsets per TRADINGEXEC_MCP_TOOLSETS
```

| Surface | Wired today | Deliberately not wired |
|---|---|---|
| `signald api` | the read routes (signals, pending orders, kill switch, config hash) and `POST /v1/halt` | propose/simulate/submit/cancel/ceiling → `503 api_disabled` (no broker adapter yet) |
| `signald mcp` | the read tools, `halt_trading` (never gated), proposal/approval stores under `audit/` | `simulate_order`/`propose_order` → `simulate_unavailable`; `submit_order`/`set_sleeve_allocation` → not offered unless their toolset is listed, then `*_unavailable` |

Broker-backed reads (`account`, `positions`, `risk`, `sleeves`) answer `null` until the P5
adapters exist — an absent number, never an invented one. The API maps its one configured key
to the `admin` role; role-scoped keys belong in the OS keystore (plan §7.2) before any mutating
route is wired.

---

## Incident runbook

One row per alert in §9.4.  Alerts are emitted to the notifier (Discord webhook,
`TRADINGEXEC_NOTIFIER_URL`); test delivery with `py -3.12 -m signald notify-test`.

| Alert | First action |
|---|---|
| `gate_exception` | The order is already blocked. Fix the raising check and re-run the gate test suite before re-enabling. |
| `reconcile_drift` | HALT the sleeve. Do **not** "fix" the local ledger to match; investigate the fill stream. |
| `protective_stop_missing` | Flatten that position, then investigate the order-state machine. |
| `flat_verification_failed` (15:55+) | Use the closing auction / marketable limit; escalate until verified flat; page. |
| `clock_offset` (>50 ms) | No new orders; fix NTP; verify the ledger has no backwards timestamps. |
| `data_stale` (>2 s quotes / >60 s bars) | Quarantine the instrument; if systemic, stop new intraday entries for the session. |
| `spread_blowout` (>3× median) | Quarantine the instrument; the cost gate should refuse it; verify the gate fired. |
| `broker_error_storm` (≥3 failures/60 s) | Kill switch; reconcile once the broker is healthy. |
| `daily_loss_soft` | Halve new size; confirm the ladder rung that fired. |
| `daily_loss_hard` | Flatten intraday; no re-entry; post-mortem written before re-arm. |
| `derisk_rung` | Confirm the ladder rung (5-day / DD / halt-DD) and the size it imposed. |
| `halt_entered` | No entries for the 5-minute reopen cooldown; exits only. |
| `duplicate_client_order_id` | Stop the submitting loop; query the broker for the in-flight id before any retry. |
| `kill_switch` | Confirm the sentinel is present and the episode latch written; begin re-arm only after a post-mortem. |
| `sleeve_ceiling_hit` | Expected on a full sleeve; no action unless it fires weekly without a novel cause. |
| `heartbeat_loss` | Assume the daemon is down; `py -3.12 -m signald status`, inspect `signals/signald_daemon.log` and `signals/signald.pid`, then `schtasks /run /tn Signald_Daemon` (the PID lock refuses a second instance if it was only wedged — end the task first, `schtasks /end /tn Signald_Daemon`). |

---

## Kill switch and re-arm

Halt is a filesystem sentinel plus a persisted episode latch (`TRADINGEXEC_KILL_SWITCH`, default
`./kill_switch`; latch `./audit/halt_episode.json`).  Re-arm is **manual only**.

Halt sequence — in order:

1. `py -3.12 -m signald halt` — write the sentinel. Equivalents: MCP `halt_trading`
   (`signald mcp`) or `POST /v1/halt` (`signald api`); all three engage the same switch.
2. Cancel working orders: `py -3.12 -m signald flatten --cancel-only` **(planned: no CLI command yet)**.
3. Flatten intraday: `py -3.12 -m signald flatten --intraday` **(planned: no CLI command yet)**.
4. Freeze new entries: the sentinel already suppresses emission; confirm `py -3.12 -m signald status`
   shows `kill_switch: HALTED`.
5. Write the episode latch (done automatically on halt): verify `./audit/halt_episode.json` exists and
   carries `episode` + `since`.

Re-arm sequence — in order:

1. Write the post-mortem first; re-arm requires its reference id.
2. `py -3.12 -m signald halt --resume --post-mortem <ref>` — clears the sentinel; the episode
   record is **retained** as the audit trail.
3. Restore size in **staged** steps across sessions: 25% → 50% → 100%, each step subject to the vol
   target and the drawdown ladder.  Never restore in one jump.
4. Confirm `py -3.12 -m signald status` shows `kill_switch: armed` and a fresh heartbeat.

---

## Mandate change (widen / narrow the allowed list)

`mandate.json` is hash-pinned: a hand-edited file is **rejected at load**, so widening or narrowing
the symbol list goes through the re-signing verbs (`signald/mandate.py`), which archive the old
mandate id and write atomically.

```bash
py -3.12 -m signald mandate-add NFLX --reason "candidate card 2026-09-15"
py -3.12 -m signald mandate-remove NFLX --reason "position closed"
py -3.12 -m signald mandate-remove NFLX --force --reason "holdings feed down"
```

- `mandate-add <SYMBOL>` re-signs with the symbol added. A running daemon picks it up on its next
  poll — the loop re-reads the mandate each cycle, audited as `mandate_reloaded` — so no restart.
  Refusal rows carry the mandate hash they were judged under, so artifacts refused only for the
  symbol check are **re-opened exactly once** by the widening; emitted signals never replay.
- `mandate-remove <SYMBOL>` refuses while the symbol is held or while holdings cannot be verified:
  without a provable position the gate cannot exempt the exit, so the name would be stranded.
  `--force` overrides; the printed warning is real — the name stops taking entries and an
  unprovable holding keeps its exit blocked.
- Both take `--operator` / `--reason` (recorded in the archive row) and the daemon's
  `--data` / `--mandate` / `--env` overrides. **Pass the daemon's `--data`**: the
  `mandate_added` / `mandate_removed` audit row lands in that data dir's ledger, so pointing the
  verb at another dir leaves the running daemon without a record of the change.
- The executor never widens its own mandate. A strongly-rated buy for a not-held name it bars is
  queued in `<data>/mandate_candidates.jsonl`, audited as `mandate_candidate`, and paged as a card
  carrying the exact promotion command.

---

## Promotion checklist

Paper → live (tiny).  `promotion_report()` in `signald/promotion.py` machine-checks every line; a
failing line is a blocker, never a warning.  Do not promote with any blocker outstanding.

- [ ] ≥20 paper sessions, all reconciled, all flat-by-close, zero unexplained drift.
- [ ] D1–D12 drills passed, with the drill log committed.
- [ ] Validation gates met for every enabled setup (DSR ≥ 0.95 / PBO ≤ 0.05 / ≥50 trades / ≥6 months OOS) with N recorded.
- [ ] Cost model validated against ≥50 realized paper fills (slippage distribution, not a point estimate).
- [ ] Kill switch, daily-loss flatten, and stale-data block each demonstrated live (paper).
- [ ] Hard capital cap configured; approval thresholds set; two independent opt-ins in place.
- [ ] Backups + restore drill done; alerting verified; runbook printed/committed.
- [ ] Owner signs the pre-registered kill/shrink criteria date.

Evidence check (given the P4 harness validation mapping and the ops booleans):

```
py -3.12 -c "from signald.promotion import promotion_report as r; print(r(profile='live', sessions_reconciled=20, drills_passed=[f'D{i}' for i in range(1,13)], required_drills=[f'D{i}' for i in range(1,13)], validation={'ORB_RVOL':{'dsr':0.96,'pbo':0.04,'trades':80,'oos_months':8,'n':12}}, kill_switch_tested=True, daily_loss_tested=True, stale_data_tested=True, backups_tested=True, hard_cap_usd=25000, approval_threshold_usd=5000, owner_signed_date='2026-09-12'))"
```

---

## Backup / restore drill (RTO/RPO)

Backups land under `TRADINGEXEC_BACKUP_DIR` (default `./backups`); the durable state is the audit
ledger, the journal, the signal feed and the mandate.

1. Stop the daemon cleanly (Ctrl-C; the PID lock releases).
2. Copy `./audit/`, `./signals/`, `./mandate.json` to the backup dir.
3. Restore onto a clean host into a fresh `./audit/` + `./signals/`.
4. `py -3.12 -m signald verify --audit ./audit/audit.jsonl` — chain must verify.
5. `py -3.12 -m signald status` — reconcile against the broker; **zero drift** is the pass condition.
6. Record measured **RTO** (wall-clock to verified-flat service) and **RPO** (ledger timestamp of the
   last row vs the backup moment) in the drill log.

Drill D9 (plan §9.3) is this procedure; it must pass before promotion.

---

## Drills

Run before any promotion, then one per week on rotation (plan §9.3, D1–D12).  Record every result in
the drill log and commit it; a promotion without a committed log is blocked.  D12 (no-broker CI)
requires the full suite to pass with credentials absent and outbound access denied.
