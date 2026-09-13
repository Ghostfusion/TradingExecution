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
| 15:45–15:55 | flatten intraday; verify flat against the broker; closing-auction preference where eligible | `py -3.12 -m signald flatten` **(planned)** |
| 16:15 | post-close: reconcile fills vs positions vs journal; compute the day's scorecard; write the audit summary | `py -3.12 -m signald scorecard` **(planned)** |
| 17:00 | report: sleeve PnL, slip vs model, gate histogram, data-quality summary | `py -3.12 -m signald scorecard --report` **(planned)** |

Every loop writes a heartbeat; a stale heartbeat pages via the watchdog:
`py -3.12 -m signald watchdog`.

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
| `heartbeat_loss` | Assume the daemon is down; `py -3.12 -m signald status`, inspect `signald.pid`, restart under the PID lock. |

---

## Kill switch and re-arm

Halt is a filesystem sentinel plus a persisted episode latch (`TRADINGEXEC_KILL_SWITCH`, default
`./kill_switch`; latch `./audit/halt_episode.json`).  Re-arm is **manual only**.

Halt sequence — in order:

1. `py -3.12 -m signald kill` **(planned: MCP `halt_trading` or CLI)** — write the sentinel.
2. Cancel working orders: `py -3.12 -m signald flatten --cancel-only` **(planned)**.
3. Flatten intraday: `py -3.12 -m signald flatten --intraday` **(planned)**.
4. Freeze new entries: the sentinel already suppresses emission; confirm `py -3.12 -m signald status`
   shows `kill_switch: HALTED`.
5. Write the episode latch (done automatically on halt): verify `./audit/halt_episode.json` exists and
   carries `episode` + `since`.

Re-arm sequence — in order:

1. Write the post-mortem first; re-arm requires its reference id.
2. `py -3.12 -m signald rearm --postmortem <ref>` **(planned)** — clears the sentinel; the episode
   record is **retained** as the audit trail.
3. Restore size in **staged** steps across sessions: 25% → 50% → 100%, each step subject to the vol
   target and the drawdown ladder.  Never restore in one jump.
4. Confirm `py -3.12 -m signald status` shows `kill_switch: armed` and a fresh heartbeat.

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
