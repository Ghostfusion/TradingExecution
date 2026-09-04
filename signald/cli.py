"""signald CLI — run/verify/status/sample/approve (plan §4 + Phase A gates).

Exit codes: 0 ok, 1 runtime error, 2 usage, 3 already running.
Default mode is DRY-RUN (logs what WOULD be emitted, writes nothing);
``--execute`` opts into signal persistence. No order path exists in Phase A.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .alpaca_ref import AlpacaReference
from .config import Config, load_config
from .daemon import AlreadyRunning, DaemonLock, touch_heartbeat
from .kill_switch import is_halted
from .mandate import DEFAULT_MANDATE, MandateError, load_mandate, write_mandate
from .notifier import Notifier
from .processor import SignalProcessor
from .samples import write_sample
from .stores import AuditChain, Journal, SignalStore
from .watch import WatchLoop
from .watchdog import DEFAULT_MAX_AGE_S, check_heartbeat


def _build(cfg: Config) -> tuple[SignalProcessor, AuditChain]:
    mandate = load_mandate(cfg.mandate_path)
    audit = AuditChain(cfg.audit_file, cfg.now)
    journal = Journal(cfg.journal_file, cfg.now)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    reference = AlpacaReference(
        api_key=cfg.alpaca_key,
        secret=cfg.alpaca_secret,
        paper=cfg.alpaca_paper,
        transport=cfg.transport,
    )
    notifier = Notifier(
        url=cfg.notifier_url,
        timeout_s=cfg.notifier_timeout_s,
        retries=cfg.notifier_retries,
        transport=cfg.transport,
        now=cfg.now,
    )
    processor = SignalProcessor(cfg, mandate, store, journal, audit, reference, notifier)
    return processor, audit


def _state_overrides(args: argparse.Namespace) -> dict:
    """Anchor state files under --data so CLI runs are self-contained."""
    ov = {}
    if getattr(args, "data", None):
        base = Path(args.data)
        ov.update(
            audit_file=base / "audit" / "audit.jsonl",
            journal_file=base / "audit" / "journal.jsonl",
            halt_latch_path=base / "audit" / "halt_episode.json",
            heartbeat_path=base / "audit" / "heartbeat",
            pid_file=base / "signald.pid",
        )
    return ov


def _ensure_dirs(cfg: Config) -> None:
    for d in (cfg.watch_dir, cfg.data_dir, cfg.audit_file.parent):
        Path(d).mkdir(parents=True, exist_ok=True)


def cmd_run(args: argparse.Namespace) -> int:
    ov = {
        "dry_run": not args.execute,
        "poll_seconds": args.poll,
    }
    if args.watch:
        ov["watch_dir"] = args.watch
    if args.data:
        ov["data_dir"] = args.data
    ov.update(_state_overrides(args))
    if args.kill_switch:
        ov["kill_switch_path"] = args.kill_switch
    if getattr(args, "mandate", None):
        ov["mandate_path"] = args.mandate
    cfg = load_config(env_file=args.env, **ov)
    _ensure_dirs(cfg)
    if cfg.alpaca_key is None and cfg.transport is None:
        print("error: ALPACA_API_KEY not set and no transport seam (reference data is required)",
              file=sys.stderr)
        return 1
    try:
        processor, _audit = _build(cfg)
    except MandateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    loop = WatchLoop(processor, cfg.poll_seconds)

    try:
        with DaemonLock(cfg.pid_file):
            if args.once:
                results = loop.run_once()
                for r in results:
                    print(f"[{r.kind}] {_describe(r)}")
                return 0
            print(f"signald {_now_str(cfg)} mode={'dry-run' if cfg.dry_run else 'signal'} "
                  f"watch={cfg.watch_dir} (kill switch: {cfg.kill_switch_path})", flush=True)
            while True:
                touch_heartbeat(cfg.heartbeat_path)
                for r in loop.run_once():
                    print(f"[{r.kind}] {_describe(r)}", flush=True)
                import time as _t
                _t.sleep(cfg.poll_seconds)
    except AlreadyRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0


def cmd_verify(args: argparse.Namespace) -> int:
    audit = AuditChain(Path(args.audit), datetime.now)
    ok, _idx, detail = audit.verify()
    print(f"{'OK' if ok else 'CORRUPT'} — {detail}")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    ov = _state_overrides(args)
    if args.watch:
        ov["watch_dir"] = args.watch
    if args.data:
        ov["data_dir"] = args.data
    cfg = load_config(env_file=args.env, **ov)
    journal = Journal(cfg.journal_file, cfg.now)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    audit = AuditChain(cfg.audit_file, cfg.now)
    rows = journal.replay()
    signals = store.read_all()
    print(f"journal rows: {len(rows)} | signals emitted: {len(signals)} "
          f"| today: {journal.signals_today_count()} | audit rows: {len(audit.read())} "
          f"| kill_switch: {'HALTED' if is_halted(cfg.kill_switch_path) else 'armed'}")
    if signals:
        last = signals[-1]
        print(f"last: {last['signal_id']} {last['ticker']} {last['action']} "
              f"@{last['emitted_at']} verdict={last['gates']['verdict']}")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    ov = _state_overrides(args)
    if args.watch:
        ov["watch_dir"] = args.watch
    if args.data:
        ov["data_dir"] = args.data
    cfg = load_config(env_file=args.env, **ov)
    _ensure_dirs(cfg)
    target = Path(args.out) if args.out else Path(cfg.watch_dir) / "research_decision.json"
    p = write_sample(target, ticker=args.ticker, direction=args.direction,
                     allocation_pct=args.allocation)
    print(f"wrote sample decision: {p}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    ov = _state_overrides(args)
    if args.watch:
        ov["watch_dir"] = args.watch
    if args.data:
        ov["data_dir"] = args.data
    cfg = load_config(env_file=args.env, **ov)
    audit = AuditChain(cfg.audit_file, cfg.now)
    signal_id = args.signal_id or ""
    audit.append(f"approval_{args.action}", f"operator {args.action} for {signal_id}",
                 signal_id=signal_id, operator=args.operator)
    print(f"recorded approval_{args.action} for {signal_id or 'unknown signal'} "
          "(execution gate lands in M1; Phase A emits signals only)")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    cfg = load_config(env_file=args.env, **({"notifier_url": args.url} if args.url else {}))
    notifier = Notifier(url=cfg.notifier_url, timeout_s=cfg.notifier_timeout_s,
                        retries=cfg.notifier_retries, transport=cfg.transport, now=cfg.now)
    if not notifier.enabled:
        print(
            "notifier: not configured (set TRADINGEXEC_NOTIFIER_URL or pass --url)",
            file=sys.stderr,
        )
        return 1
    event = {"event": "signal", "ts": cfg.now().isoformat(timespec="seconds"),
             "signal_id": "test", "detail": "notify-test from signald CLI"}
    ok = notifier.send(event)
    print(f"notifier: {'delivered' if ok else 'FAILED'}")
    return 0 if ok else 1


def cmd_watchdog(args: argparse.Namespace) -> int:
    cfg = load_config(
        env_file=args.env,
        **({"heartbeat_path": args.heartbeat} if args.heartbeat else {}),
    )
    notifier = Notifier(url=cfg.notifier_url, timeout_s=cfg.notifier_timeout_s,
                        retries=cfg.notifier_retries, transport=cfg.transport, now=cfg.now)
    fresh = check_heartbeat(cfg.heartbeat_path, notifier, max_age_s=args.max_age)
    print(
        f"watchdog: {'fresh' if fresh else 'LOSS'} "
        f"(max_age {args.max_age}s, heartbeat {cfg.heartbeat_path})"
    )
    return 0 if fresh else 1


def cmd_init_mandate(args: argparse.Namespace) -> int:
    import os
    target = Path(args.mandate) if args.mandate else Path(
        os.getenv("TRADINGEXEC_MANDATE_PATH", "mandate.json")
    )
    m = write_mandate(target, DEFAULT_MANDATE)
    print(f"wrote mandate {m.id} (hash {m.hash[:12]}…) -> {target}")
    return 0


def _describe(r) -> str:
    if r.envelope is None:
        return "; ".join(r.reasons) or r.kind
    e = r.envelope
    return f"{e['ticker']} {e['action']} target_pct={e['target_pct']} " \
           f"verdict={e['gates']['verdict']} signal_id={e['signal_id']}"


def _now_str(cfg: Config) -> str:
    return cfg.now().isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="signald", description="TradingExecution signal daemon (Phase A)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="poll the decisions inbox and emit signals")
    run.add_argument("--once", action="store_true", help="process pending artifacts once and exit")
    run.add_argument("--mandate", help="mandate JSON path (overrides env/default)")
    run.add_argument(
        "--execute", action="store_true", help="opt in: persist signals (default is dry-run)"
    )
    run.add_argument("--watch", help="decisions inbox directory")
    run.add_argument("--data", help="signals output directory")
    run.add_argument("--kill-switch", help="kill-switch sentinel path")
    run.add_argument("--env", help=".env file path")
    run.add_argument("--poll", type=float, help="poll interval seconds")
    run.set_defaults(func=cmd_run)

    v = sub.add_parser("verify", help="verify the audit chain (SHA-256)")
    v.add_argument("--audit", default=None, help="audit file path")
    v.set_defaults(func=cmd_verify)

    st = sub.add_parser("status", help="journal/signal/audit summary")
    st.add_argument("--env", help=".env file path")
    st.add_argument("--watch")
    st.add_argument("--data")
    st.set_defaults(func=cmd_status)

    sm = sub.add_parser("sample", help="write a sample research_decision.json")
    sm.add_argument("--out", help="output path (default ./decisions/research_decision.json)")
    sm.add_argument("--ticker", default="AVGO")
    sm.add_argument("--direction", default="reduce")
    sm.add_argument("--allocation", type=float, default=None)
    sm.add_argument("--env")
    sm.add_argument("--watch")
    sm.add_argument("--data")
    sm.set_defaults(func=cmd_sample)

    nt = sub.add_parser("notify-test", help="send a test webhook to the configured notifier")
    nt.add_argument("--url", default=None, help="override webhook URL")
    nt.add_argument("--env")
    nt.set_defaults(func=cmd_notify_test)

    wd = sub.add_parser(
        "watchdog", help="check daemon heartbeat; dispatch heartbeat_loss when stale"
    )
    wd.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S)
    wd.add_argument("--heartbeat", default=None)
    wd.add_argument("--env")
    wd.set_defaults(func=cmd_watchdog)

    ap = sub.add_parser("approve", help="record operator approval/rejection (execution in M1)")
    ap.add_argument("action", choices=["approve", "reject"])
    ap.add_argument("--signal-id", default="")
    ap.add_argument("--operator", default="vince")
    ap.add_argument("--env")
    ap.set_defaults(func=cmd_approve)

    im = sub.add_parser("init-mandate", help="write a fresh signed mandate")
    im.add_argument("--mandate", default=None)
    im.set_defaults(func=cmd_init_mandate)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
