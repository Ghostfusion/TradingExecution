# Changelog — TradingExecution

Format follows the TradingAgents repo (date-stamped entries, concise what/why).

## 2026-09-29 — the audit ledger: cleared on request, and the append race that was already in it

The owner asked for the 152,369 `deduped` rows to be cleared. Compacting a SHA-256 chained ledger surfaced a
worse defect underneath, and the pair is recorded together because the second is why the first was needed twice.

**The original ledger was already corrupt, and not by an edit.** The archived original fails `verify` at rows
66,520, 66,521, 66,527 and 66,528 — every break a `prev_mismatch`, every rogue row a `mandate_removed`:

```
[66519] deduped          prev=dadd2a… hash=096cb9…
[66520] mandate_removed  prev=dadd2a…   <- same tail: a second process linked to it
[66521] deduped          prev=096cb9…   <- the daemon carried on from its own row
```

`AuditChain.append` read the tail, linked to it, then wrote. The daemon and a `signald mandate-remove` run
against the same `--data` are two processes doing that, so they read the **same** tail and forked the chain.
`verify` then reported CORRUPT — the one message that must always mean tampering, spent on a race.

- **`signald/stores.py::_cross_process_lock`** — `append` now takes a sidecar lock around read+write, with
  stale-holder takeover like `DaemonLock`, and **fails open after 5 s** so a wedged holder can never stall the
  daemon. The trade is deliberate and stated in the code: a raced append is still reported by `verify`, a stopped
  daemon is silent. Test: `tests/test_stores.py::test_concurrent_appends_do_not_fork_the_chain` — two writers,
  the read slowed to widen the read→write gap; fails before the fix (row 1 already CORRUPT, 2/2 runs), passes
  after. Suite 1138 green, ruff clean.
- **The compaction.** Original archived to `signals/audit/audit.jsonl.pre-dedup-20260929.bak` (unmodified, chain
  as written), the surviving 1,548 rows re-linked and verified, then one `ledger_compacted` row appended through
  the normal `AuditChain.append` path — which also proves the re-linked chain is appendable:

```
153,917 rows / 82.7 MB  ->  1,549 rows / 734 KB     (dropped 152,369 deduped)
signald verify --audit signals/audit/audit.jsonl  ->  OK — 1549 rows chained
```

- **`AuditChain.append` re-reads the whole ledger to find the tail**, so the flood was not only 82 MB of noise:
  every append was O(n) on top of it, and the daemon's heartbeat was touching **67–69 s** apart while it ran on
  153k rows. Compaction is what puts the poll cycle back on the poll interval — measured after the restart,
  **13 heartbeat touches in 120 s, gaps of 10.3 s**.
- **A trap found while verifying.** `signald verify` / `status` with no `--data` read the **config default**
  `./audit/audit.jsonl`, not the ledger the daemon writes (`--data .\signals` → `signals/audit/audit.jsonl`).
  Both printed `OK — 5 rows chained` against a file the daemon never touches. Documented in
  `docs/RUNBOOK.md` (with the compaction procedure) and `docs/AGENT_ONBOARDING.md` §2.
- **Not deleted:** the 82.7 MB archive stays. It is the only copy of the fork evidence and of everything the
  daemon decided before today.

## 2026-09-29 — a restart is two steps: `schtasks /end` does not stop the daemon

`docs/RUNBOOK.md` documented `/end` as ending "the process tree" and the `heartbeat_loss` recovery as one
`schtasks /run`. Observed live while restarting to pick up the ledger fix:

- `schtasks /end /tn Signald_Daemon` returned SUCCESS and terminated the task's `cmd.exe` wrapper. The **python
  daemon it launched survived**, kept the PID lock and kept heartbeating.
- The immediate `schtasks /run` started a second `cmd.exe`, hit `DaemonLock.acquire`'s `AlreadyRunning`, and
  exited **`Last Result: 1`** — with the task reading `Status: Ready` and the *pre-restart* code still serving
  the watch tree. A restart that reports success and is not one.
- The working sequence: `/end` → `taskkill /PID <pid> /T /F` → `/run` → verify a **new** pid in
  `signals/signald.pid` and an advancing heartbeat. Executed 11:42 CT: pid 28108 → 17380, task `Running`,
  heartbeat fresh, kill switch armed.

`docs/RUNBOOK.md` is corrected in both places (supervision section, `heartbeat_loss` row). No daemon behaviour
changed — `/end`'s scope is Task Scheduler's, not ours — but an operator following the documented line would
have left the daemon down and believed it running.

The restart is also the production proof of the entry below: **4 rows in 3.5 minutes** against 46–197/minute
before, all four real decisions on a newly-arrived artifact (INCY: `admitted` → `rejected` → `monitor` →
`quarantined`), and **zero `deduped`**.

## 2026-09-29 — the inbox's dedupe rows were growing the audit ledger without bound

The 2026-09-15 fix silenced the processor's duplicate skip — "the ledger records decisions, not poll cycles" —
and it held for that path. It stopped being reachable on **2026-09-18**, when `cli._build` started constructing
the `Inbox`: a re-seen artifact now exits at the boundary, *before* the `is_processed` guard could return, and
`Inbox.admit` wrote a `deduped` audit row of its own at each of its three dedupe sites. Measured live on the
daemon's ledger:

```
signals/audit/audit.jsonl   153,513 rows, 82.4 MB
  deduped                   151,976   three reasons, all Inbox._audit_row
  rejected_invalid            1,327
  every other kind            1,537
across 72 distinct artifacts — ~2,110 rows each, ~3,000/hour inside the scan window
  already dead-lettered (committed)     73,427
  already admitted (committed)          44,930
  already dead-lettered (dead_lettered) 33,640
```

- **`signald/inbox.py`** — the three `_audit_row("deduped", …)` calls are gone. A replay still returns
  `Admission(DEDUPED, …)`, so the effectively-once invariant is untouched, but it writes nothing: the durable
  record already exists as the `inbox.jsonl` state row plus the `admitted` / `dead_lettered` ledger row from the
  first admission. Nothing else in `admit()` changes.
- **"Never silent" is unaffected.** It covers unreadable, non-compliant, hash-mismatched and expired artifacts
  (dead-lettered with a `reason_code`) and non-permissible signals (quarantined) — all still audited, once.
  A poll of an unchanged file is not a decision, which is the distinction the boundary was missing.
- **Tests** — `tests/test_inbox.py::test_replays_do_not_grow_the_audit_ledger` replaces
  `test_admission_and_duplicate_are_both_audited`, which pinned the flood; `test_unknown_major_is_dead_lettered`
  now expects one row rather than two. `tests/test_processor.py::test_the_daemons_boundary_does_not_grow_the_ledger_either`
  covers the shape production actually builds — the pre-existing no-growth test builds the processor *without* an
  inbox, which `cli._build` never does. Both new tests fail before the fix (five replays appended five rows;
  `assert 8 == 3`). Full suite **1137 green**, ruff clean.
- **Not pruned.** The 82 MB already written stays. The ledger is append-only and SHA-256 chained, so clearing it
  is a destructive operation on operator state, not a bug fix — flagged for the owner instead.
- **The running daemon keeps appending until it is restarted**; it holds the pre-fix code. Its heartbeat is live
  and its scan window is open.

## 2026-09-28 — the promotion queue is readable from `signald status`

`mandate_candidates.jsonl` was visible only by opening the file: a strongly-rated buy the mandate bars was
queued, audited and paged, and then the operator still had to know where the JSONL lived to see the queue.
`signald status` now prints it, above the hold ledger.

Each line names the exact command the Discord card carries. The point of the queue is that a human promotes
it, so the read-out has to be actionable rather than a bare count:

```
candidates (promotable, outside the mandate): 1
  NFLX BUY (Buy) -> signald mandate-add NFLX  @2026-09-28T12:00:00
```

Newest five, matching the hold ledger. `signald/cli.py::_candidate_summary` is pure, so the wording is
testable without a terminal. Tests: `tests/test_cli.py` (3) — the read-out end-to-end through
`cli.main(["status", ...])` with both ledgers seeded, the both-empty case, and the command-plus-cap unit test.
Full suite 1136 green.

## 2026-09-28 — every HOLD is recorded, in the mandate or out of it (owner instruction)

A `hold` carries information even when it is not tradable, and the mandate gate used to end the story one of
two ways: an **in-mandate** hold emitted a HOLD signal (visible), while a hold on a name the mandate bars was
**refused and then dropped** — no signal (correctly), no queue entry (`PROMOTABLE_RATINGS` is buy/overweight
only, so `_offer_candidate` ignores it), and nothing durable that said research had said "hold".

- **`signald/stores.py::MonitorStore`** — an append-only `monitor.jsonl` under the data dir, one row per
  `(ticker, decision_hash)`, mirroring `CandidateStore`. The row records `in_mandate` and `blocked` rather than
  leaving them to be inferred: the same hold reads differently when the name is tradable ("holding what I own")
  and when the mandate bars it ("research likes it, I cannot buy it").
- **`signald/processor.py::_record_monitor`** writes it on **both** paths — after the refusal audit in the
  BLOCK branch and after the envelope on the emitted path — so the ledger holds every hold regardless of
  mandate membership. It fires on `contract.action == "HOLD"` only, dedupes on `(ticker, decision_hash)`, and
  is written before the `--dry-run` return, exactly like the candidate queue: it records what research said,
  it is not a trading side effect.
- **Not tradability.** Nothing on this path touches the signal store, the gate or the order path — a blocked
  hold stays blocked and its `envelope` stays `None`. That is the invariant the tests are built around.
- **`signald/notifier.py::monitor_event`** plus a `discord_event` branch, paged only when the hold is blocked
  **and** the symbol is outside the mandate. The other two cases already reach the operator — an in-mandate hold
  as its own HOLD signal when it passes, or as that gate's error card when it does not — so paging here as well
  would duplicate them. The event carries no `signald mandate-add` command: a hold is not a promotion request.
- **`signald status`** prints the ledger: total holds, how many are outside the mandate, and the newest five.
  Folded into `status` rather than given its own subcommand — it is a read-out, and the promotion queue is
  shown beside it (see the entry above).
- **One defect, found by the live smoke and fixed in the same pass.** The audit sentence and the `status` line
  both derived which side of the mandate a hold was on from `blocked`, so an in-mandate hold refused by some
  *other* gate was recorded and printed as "outside the mandate" — measured live against the owner's own
  mandate: NVDA, `in_mandate=True`, blocked on `data`. The structured field was right and the sentence was not,
  and the sentence is what an operator reads. Both now key on `in_mandate` and name the binding gate
  ("inside the mandate, blocked on data"). Pinned twice — `tests/test_monitor.py` covers the audit reason and
  `_monitor_summary`.
- Tests: `tests/test_monitor.py` (10) — the store's keying, the out-of-mandate hold recorded *and still
  untradable*, the in-mandate hold recorded too, a reduce NOT monitored, one row per decision across polls
  (with the journal dedupe disabled to exercise the store's own), the truthful audit reason for the
  in-mandate-but-blocked case, the page going only to the mandate-barred case, the status read-out's counting
  and wording, and the new event rendering a real card instead of the empty generic line.

## 2026-09-27 — the artifact's `net_beta` now reaches `BookState.net_beta` (RISK-4/PLAN-7)

The research artifact (`research_decision.json`) declares a book net beta `sum(w_i*beta_i)` as a new
**envelope** key (schema `1.2.0`), and this repo was the missing *writer* — `BookState.net_beta` was declared
with a `0.0` default and every construction site omitted it, so a reader could not tell "flat book" from "not
measured".

- **`signald/schema.py`** parses/validates it into `ResearchDecision.net_beta` (a number or `None`; a
  non-numeric value raises `ContractError`), and the field is removed from the `extra` catch-all.
- **`signald/contracts.py`** rejects a non-numeric `net_beta` at the boundary (`invalid_net_beta`), the same
  fail-closed shape as `opportunity_score`.
- **`signald/gates.py::build_context`** carries `rd.net_beta` onto the decision's `BookState` (the book the
  house gate reads), and **`signald/engine.py::book_with_trades`** propagates it so a re-derived book does not
  drop it. `contracts/research_decision.v1.schema.json` was refreshed byte-identical to the engine's vendored
  copy.
- `None` means unknown, **never `0.0`**: an artifact that omits the field (or ships `null`, which the engine
  does today — its run state has no configured-basket beta producer) leaves `BookState.net_beta` `None`.
- Tests: `tests/test_gates.py` (build_context carries the number / leaves `None` / parser rejects a string) and
  `tests/test_contracts.py` (boundary accepts a numeric, rejects a non-numeric). Affected suites and
  `ruff check` green.

## 2026-09-25 — the close window is now named in the scan, and the lint step is green again

The owner's playbook asks for the decision run at **15:45–15:55 ET** ("act on the
close"). The daemon already scans all of the regular session, so a report written
in that window was already picked up — what was missing was any way to TELL a
close-adjacent pickup from a mid-session one, in the log or afterward.

- **`calendar.close_window(stamp, minutes)`** — is this minute inside the last
  `minutes` of the SESSION, and how many remain. Measured to that day's own bell
  (`Session.close_et`, 16:00 or 13:00 on a half day), so "the last 15 minutes"
  means 15:45–16:00 normally and 12:45–13:00 on a half day. Returns `(False, None)`
  outside a session, past the bell, or before the window opens; `minutes <= 0`
  disables it.
- **`Config.close_window_minutes = 15`** (`TRADINGEXEC_CLOSE_WINDOW_MINUTES`; 0
  disables) and `WatchLoop.scan_window` reports the phase `rth_close` with
  `CLOSE WINDOW (Nm to the 16:00 bell)` in the detail. **The window still
  SCANS** — it is a label, not a gate, and the RTH gate is unchanged.
- **The loop announces an open-phase reason change** (rth -> rth_close) once, the
  same way it already announced each closed phase, so the window's arrival is one
  line in the daemon log rather than something an operator has to infer from
  timestamps. The reopen line and the closed-phase lines are unchanged.
- Nothing about orders changed: the mandate is still checked first, `signald
  flatten` still carries the closing-auction TIF, and the guard's TIF allowlist
  (`ALLOWED_TIFS = ("day", "gtc")`) does **not** include `cls` — that mismatch is
  a safety-surface decision for the owner, not something this change touches.
- **The lint step is green again.** `ruff check signald/ tests/` (the CI lint
  command) was failing on six PRE-EXISTING errors in files this change does not
  otherwise touch: an unused `datetime` import in `signald/cli.py`, three
  ambiguous `l` loop variables plus a 102-char line in `tests/test_cli.py`, and
  an unsorted import block plus two 102-char lines in `tests/test_processor.py`.
  An unsorted-import autofix and five small edits; no behaviour changed.
- **`ALLOWED_TIFS` now includes the closing-auction TIF** (owner decision
  2026-09-25): `("day", "gtc", "cls")`. `flatten.py` plans a close-routed leg
  with `tif="cls"` (its `CLOSING_AUCTION_TIF`), and the guard's allowlist did
  not contain it - latent while flatten is not routed through the guard, but the
  two constants disagreed, which is the kind of gap that only shows up on the
  day the path is wired. `opg` and the IOC/FOK family stay out: nothing in this
  book emits them. The extended-hours rule is unchanged (it still demands `day`,
  so `cls` is refused there too). Tests: +1 (the auction TIF passes legality;
  `opg`/`fok` still deny; extended hours still refuse it).
- Tests: `tests/test_watch.py` +2 (`close_window`'s own boundaries including a
  half day and the disabled case; the label being a label), full suite **1114
  passed**, `ruff check signald/ tests/` clean.


## 2026-09-18 (e) — the ingest boundary was never wired, so one bad artifact paged all morning

`reports/AMZN_20260917_172613/research_decision.json` carries `rating: null` and `direction: null` (the emitter
plan says a field with no source stays `null` and the artifact is still emitted, so this is a valid artifact
with no resolvable action — the executor's rejection is correct). The daemon re-notified it **every 10 seconds**
for hours: the audit ledger holds **1327 `rejected_invalid` rows**, one per poll cycle, and Discord got a card
for each.

- **The root cause: `signald run` never built the `Inbox`.** `cli._build` constructed the processor with seven
  positional arguments and `inbox` defaults to `None`, so the dedupe table that makes one artifact produce one
  verdict — the module whose docstring is "effectively once", the one the tests and the plan both assume — was
  **never consulted in production**. The two earlier fixes for this same bug class (the 2026-09-15 refusal loop,
  the 2026-09-16 `skipped_duplicate` flood) patched the *symptoms* in the processor without noticing the boundary
  was dead. It is now built and passed, with `inbox_file` / `dead_letter_dir` / `quarantine_dir` on `Config`
  (env-overridable, anchored under `--data` with the other state paths).
- **The invalid path recorded nothing.** It is the one exit that could not use the idempotency journal — parsing
  failed, so there is no `decision_hash` — and so it re-audited and re-notified per cycle. It now keys the verdict
  on the **artifact body hash** (the same canonical fallback `Inbox.key_for` uses) and journals it: one verdict,
  one card. Keyed on content, not path, so a corrected re-emit for the same symbol is still evaluated.
- **A dead letter is a file plus an audit row per rejection**, so an envelope-invalid artifact re-dead-lettered
  every cycle too. The boundary now records the `dead_lettered` state and dedupes on re-admission; the unreadable
  case is keyed on the file's identity (`mtime_ns`, `size`) so a *rewritten* file is still examined.
- **Every terminal verdict settles its admission.** `commit` was called only on emit, so with the boundary newly
  live `Inbox.pending()` — the recovery surface for "admitted but the effect never landed" — would have listed
  every refusal in the reports tree forever. `_settle()` now closes invalid, blocked, routing-refused,
  reference-unavailable, future-dated, dry-run and journal-skipped artifacts.
- **Tests stopped modelling the bug.** The shared `processor` fixture built the processor *without* an inbox, so
  the suite exercised a shape production never had. New tests pin the production shape: the boundary dead-letters
  a no-action artifact once, settles a body-level reject once (with one page), closes a refusal, and a corrected
  re-emit still speaks. `test_unknown_major_is_dead_lettered` asserted `not inbox.seen(...)` — "nothing to
  dedupe: it never entered" — which *is* the defect; it now asserts the dedupe.
- **Live, after restart:** 11 artifacts → 6 committed, 5 dead-lettered (1 `unresolvable_action`, 4 `expired`),
  `pending() == []`, **zero `rejected_invalid` rows**, one dead-letter file per artifact. The flood is over.
- **Not changed, deliberately:** the dead-letter path does not page. The design defines it as "never silent" in
  the sense of never *discarded* — it is a file plus an audit row with a machine-readable `reason_code` — and the
  owner's emitter spec keeps a no-action artifact legal, so the producer is not at fault here. Adding a page for
  dead letters is a policy decision, not a defect fix.

Suite: 1111 passed (1105 + 6 new).

## 2026-09-16 (d) — the daemon runs from Task Scheduler, independent of any terminal

The session missed on 2026-09-16 was a daemon launched from a shell: closing the window took it down mid-day.
It now has supervision of its own, so no terminal owns it.

- **`Signald_Daemon`** (Task Scheduler) runs `run_daemon.cmd` at logon and daily 08:00 CT on weekdays, with
  restart-on-failure (every minute, up to 10), an unlimited execution-time limit, no stop-on-battery and no
  stop-on-idle, and `IgnoreNew` so a second instance can never start (the daemon's PID lock is the second line
  of defence). The first attempt pointed the task straight at a `cmd /c ... >> log` command line and never
  started: cmd's quote-stripping mangled it into `"prog" -B -m ...` and the task died with "The filename,
  directory name, or volume label syntax is incorrect". The launcher script keeps the quoting out of that path
  entirely.
- **Its output is durable**: stdout/stderr append to `signals/signald_daemon.log` (unbuffered). The steady
  state is now a handful of lines a day — the report-on-change filter from (c) is what makes that true, and
  the running daemon shows it: banner plus one line per artifact, then silence.
- **Stated, not implied**: the task is interactive-token and least-privilege, so it needs the operator logged
  on (the same shape as the existing `TradingAgents_NightlyReview` / `_StrategyQuality` tasks); a machine-wide
  NSSM service is the move if the daemon must survive a logoff. The watchdog task, the mandate and the audit
  ledger are unchanged.
- The hub supervision recorded in (c) is retired: `hub` supervises the batch runs, not production. An operator
  who wants the daemon up now uses `schtasks /run /tn Signald_Daemon`.

Suite: 1104 passed (unchanged — this entry adds a launcher and docs, no library behaviour).

## 2026-09-16 (c) — a missed session: the dead-man's switch was never scheduled, and the poll log drowned the evidence

The daemon process was gone for a whole regular session and nothing paged. Found while checking on it: the
hub record said "exited without an exit code" with the process and the LSP mux and the control surface all
dying in the same window (a host-level teardown, not a crash — the log carries no traceback). Restarted under
the PID lock; it processed the backlog within two seconds (IEI blocked, `NVDA REDUCE` emitted, `sg-48070cd2-001`)
— proving the artifacts had never been evaluated, and that nothing was double-fired, since the signal store and
the idempotency journal were both empty for them.

- **Report a transition, not a poll cycle.** `WatchLoop.run_forever` did not key anything on the artifact, so
  every handled artifact still sitting in `reports/` was re-announced on every poll: three of them wrote ~26k
  identical `[skipped_duplicate]` lines a day, which is what buried the evidence here. `ProcessResult` now
  carries the artifact `path` and the loop reports a result only when it differs from the last one reported for
  that path — unchanged means silence. `run --once` still prints every result: it is the operator override,
  and it runs once. Verified on the live tree: 3 lines on the first cycle, 0 on the next two.
- **The watchdog is documentation until something schedules it — now it is scheduled.** `docs/RUNBOOK.md` gains
  a Supervision section, and Task Scheduler task `Signald_Watchdog` runs the existing
  `signald watchdog --heartbeat ./signals/audit/heartbeat` every 5 minutes, weekdays 08:00 CT for 7 h 30 m
  (working directory pinned to the repo root so `.env` supplies the notifier URL; the live heartbeat is under
  the daemon's `--data ./signals`, not the config default `audit/heartbeat` — a bare `signald watchdog` from
  another directory checks the wrong file and reports a false LOSS). First run: `Last Result 0`. A deliberate
  maintenance stop must disable the task or it pages every 5 minutes. The daemon was also moved off
  `restart=no` onto the hub with `restart=on-failure` as an interim step, so a crash came back by itself —
  retired later the same session by the Task Scheduler task in (d).
- Still open at this point, and stated in the runbook rather than left implicit: nothing restarts the daemon
  after a **host reboot**. (d) closes that for a normal logon; a reboot with nobody logging in still leaves it
  down, and that is called out in the runbook.

Suite: 1104 passed (+2: the transition filter, and results naming their artifact).

## 2026-09-16 (b) — two test defects the suite could not see: a live-state write, and a date dependency

Both were found by running the suite once UTC had rolled over. The suite was green the whole time it was
writing the operator's live state, and the date-dependency only surfaced after midnight.

- **Tests wrote live trading state.** `tests/test_engine_session.py`'s `paper_cfg` overrode behaviour but no
  PATH, and `load_config` resolves paths relative to the CWD: every run rewrote the operator's `mandate.json`
  (with `DEFAULT_MANDATE` — byte-identical by coincidence, not by construction) and appended to the live
  `signals/orders_pending.jsonl`, the approval queue the control surface reads. 80 identical AVGO rows had
  accumulated there, all stamped `2026-09-12T09:45:00` (that module's own SESSION constant). The fixture now
  routes every filesystem field into `tmp_path` — the same list the conftest `cfg` fixture overrides — and the
  polluted queue was backed up and cleared. Verified by comparing mtimes around a per-file suite run: no test
  writes outside `tmp_path` now.
- **A pinned-clock test was date-dependent.** `test_run_once_ignores_the_window_for_the_operator` wrote a
  decision dated with the REAL clock (`samples.build_sample` uses `date.today()`) while pinning the clock to the
  fixed `AFTER_CLOSE` (2026-09-15T21:00Z). Once the real UTC date passed that constant, ingest correctly
  refused the artifact — "effective_date in the future (no lookahead)" — and the test failed. `_write_decision`
  now takes an `effective_date`, and that test pins it to the pinned clock's own date. The clock itself cannot
  move: the fake quote's `ts` is the fixture's fixed `NOW` constant, so moving the pin to the next day made the
  quote 25 h stale and the gate block on `quote_age_s` instead (observed, then reverted).

Suite: 1102 passed, and the full run writes nothing outside `tmp_path`.

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