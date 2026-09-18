"""signald CLI — run/verify/status/sample/approve (plan §4 + Phase A gates).

Exit codes: 0 ok, 1 runtime error, 2 usage, 3 already running.
Default mode is DRY-RUN (logs what WOULD be emitted, writes nothing);
``--execute`` opts into signal persistence. The config default mode is
``paper`` (owner decision 2026-09-13), so the order path is *reachable*: the
engine still refuses to arm without ``execute=True``, ``live`` still needs its
second opt-in, and ``run`` below only ever emits and notifies signals.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .alpaca_ref import AlpacaReference, ReferenceUnavailable
from .config import Config, load_config
from .control import ControlSurfaceError, build_control_api, build_mcp_server, halt_now
from .daemon import AlreadyRunning, DaemonLock
from .inbox import Inbox
from .kill_switch import is_halted, resume
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
    # The ingest boundary is not optional in the daemon: it is what makes one
    # artifact produce one verdict. Without it the poll re-discovers every
    # artifact still in the reports tree and every path that cannot record a
    # verdict (invalid, dead-lettered) re-fires each cycle - the 2026-09-18
    # Discord flood, and before that the 2026-09-15 refusal loop.
    inbox = Inbox(
        cfg.inbox_file,
        cfg.dead_letter_dir,
        cfg.quarantine_dir,
        cfg.now,
        audit=audit,
    )
    processor = SignalProcessor(
        cfg, mandate, store, journal, audit, reference, notifier, inbox=inbox
    )
    return processor, audit


def _state_overrides(args: argparse.Namespace) -> dict:
    """Anchor state files under --data so CLI runs are self-contained."""
    ov = {}
    if getattr(args, "data", None):
        base = Path(args.data)
        ov.update(
            audit_file=base / "audit" / "audit.jsonl",
            journal_file=base / "audit" / "journal.jsonl",
            inbox_file=base / "audit" / "inbox.jsonl",
            dead_letter_dir=base / "dead_letter",
            quarantine_dir=base / "quarantine",
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
            scan = "rth-only 09:30-16:00 ET" if cfg.scan_rth_only else "always"
            if cfg.scan_rth_only and cfg.scan_confirm_broker_clock:
                scan += " + broker clock"
            print(f"signald {_now_str(cfg)} mode={cfg.mode} dry_run={cfg.dry_run} "
                  f"scan={scan} watch={cfg.watch_dir} (kill switch: {cfg.kill_switch_path})",
                  flush=True)
            # One loop, one behaviour: WatchLoop.run_forever owns the poll cadence,
            # the heartbeat and the regular-session scan window (2026-09-15: the
            # CLI used to inline a duplicate loop that ignored that window).
            loop.run_forever(
                on_result=lambda r: print(f"[{r.kind}] {_describe(r)}", flush=True)
            )
            return 0
    except AlreadyRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0


def cmd_verify(args: argparse.Namespace) -> int:
    # Bare `verify` built Path(None) and crashed; resolve the configured ledger
    # the way `status` does, keeping --audit as the explicit override.
    cfg = load_config(env_file=args.env, **_state_overrides(args))
    audit = AuditChain(Path(args.audit) if args.audit else cfg.audit_file, cfg.now)
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


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _symbol_arg(raw: str) -> str:
    """Normalise and sanity-check a ticker from the command line (fail closed)."""
    symbol = str(raw or "").strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"{raw!r} is not a plausible US ticker")
    return symbol


def _holdings_precheck(cfg: Config, symbol: str) -> tuple[bool | None, str]:
    """(held, detail) from the broker. ``held`` is None when holdings are unknown."""
    reference = AlpacaReference(
        api_key=cfg.alpaca_key, secret=cfg.alpaca_secret,
        paper=cfg.alpaca_paper, transport=cfg.transport,
    )
    try:
        snapshot = reference.snapshot(symbol)
    except ReferenceUnavailable as exc:
        return None, str(exc)
    if snapshot.held_symbols is None:
        return None, "the broker returned no position detail"
    if symbol in snapshot.held_symbols:
        return True, "you hold a position in it"
    return False, "no position"


def cmd_mandate_add(args: argparse.Namespace) -> int:
    """Promote a symbol into the mandate (option A: the operator widens it, never research)."""
    ov = _state_overrides(args)
    if args.mandate:
        ov["mandate_path"] = args.mandate
    cfg = load_config(env_file=args.env, **ov)
    try:
        symbol = _symbol_arg(args.symbol)
        mandate = load_mandate(cfg.mandate_path)
    except (ValueError, MandateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if symbol in mandate.allowed:
        print(f"{symbol} is already allowed (no change, hash {mandate.hash[:12]}…)")
        return 0
    try:
        doc = json.loads(Path(cfg.mandate_path).read_text(encoding="utf-8"))
        doc["symbols"]["allowed"] = sorted(set(doc["symbols"]["allowed"]) | {symbol})
        updated = write_mandate(
            cfg.mandate_path, doc,
            provenance={"actor": "operator", "action": "mandate_add", "ticker": symbol,
                        "operator": args.operator, "reason": args.reason or ""},
        )
    except (OSError, MandateError, ValueError, KeyError, TypeError) as exc:
        print(f"error: cannot re-sign the mandate: {exc}", file=sys.stderr)
        return 1
    AuditChain(cfg.audit_file, cfg.now).append(
        "mandate_added", f"{symbol} added to the mandate",
        ticker=symbol, operator=args.operator, why=args.reason or "",
        old_hash=mandate.hash, new_hash=updated.hash,
    )
    print(f"mandate {updated.id}: added {symbol} ({mandate.hash[:12]}… -> "
          f"{updated.hash[:12]}…) | allowed {sorted(updated.allowed)}")
    print("a running daemon picks this up on its next poll; artifacts already refused "
          f"for {symbol} are re-evaluated once")
    return 0


def cmd_mandate_remove(args: argparse.Namespace) -> int:
    """Drop a symbol from the mandate, guarding a position you still hold."""
    ov = _state_overrides(args)
    if args.mandate:
        ov["mandate_path"] = args.mandate
    cfg = load_config(env_file=args.env, **ov)
    try:
        symbol = _symbol_arg(args.symbol)
        mandate = load_mandate(cfg.mandate_path)
    except (ValueError, MandateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if symbol not in mandate.allowed:
        print(f"{symbol} is not in the mandate (no change, hash {mandate.hash[:12]}…)")
        return 0
    if len(mandate.allowed) <= 1:
        print("error: symbols.allowed must stay a non-empty list", file=sys.stderr)
        return 1
    held, detail = _holdings_precheck(cfg, symbol)
    if held is None and not args.force:
        print(f"error: cannot verify holdings ({detail}); pass --force to remove "
              f"{symbol} anyway - without holdings data the gate cannot exempt a "
              "held name's reduce/exit, so a live position would be stranded",
              file=sys.stderr)
        return 1
    if held and not args.force:
        print(f"error: {symbol} is in your positions ({detail}); removing it stops new "
              f"entries. Pass --force to remove it anyway.", file=sys.stderr)
        return 1
    try:
        doc = json.loads(Path(cfg.mandate_path).read_text(encoding="utf-8"))
        doc["symbols"]["allowed"] = [
            s for s in doc["symbols"]["allowed"] if str(s).upper() != symbol
        ]
        updated = write_mandate(
            cfg.mandate_path, doc,
            provenance={"actor": "operator", "action": "mandate_remove", "ticker": symbol,
                        "operator": args.operator, "reason": args.reason or "",
                        "held": held},
        )
    except (OSError, MandateError, ValueError, KeyError, TypeError) as exc:
        print(f"error: cannot re-sign the mandate: {exc}", file=sys.stderr)
        return 1
    AuditChain(cfg.audit_file, cfg.now).append(
        "mandate_removed", f"{symbol} removed from the mandate",
        ticker=symbol, operator=args.operator, why=args.reason or "",
        held=held, old_hash=mandate.hash, new_hash=updated.hash,
    )
    print(f"mandate {updated.id}: removed {symbol} ({mandate.hash[:12]}… -> "
          f"{updated.hash[:12]}…) | allowed {sorted(updated.allowed)}")
    if held:
        print(f"warning: {symbol} is still held - a reduce/exit for it is exempt from "
              "the allow-list while the position shows at the broker, but a sell "
              "cannot open a short and the name no longer accepts entries")
    elif held is None:
        print(f"warning: holdings could not be verified; if {symbol} is actually held, "
              "its reduce/exit signal stays blocked (the gate will not exempt what it "
              "cannot prove)")
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


def _artifact_under(watch: Path, *, recursive: bool) -> Path | None:
    """Newest research_decision.json under the watch dir (the probe's view)."""
    from .watch import ARTIFACT_NAME

    if not watch.exists():
        return None
    files = list(watch.rglob(ARTIFACT_NAME)) if recursive else list(watch.glob("*.json"))
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def cmd_probe(args: argparse.Namespace) -> int:
    """Validate the newest dropped artifact against the boundary contract (plan §1.2).

    The nightly probe: it answers "would the executor accept what the research
    layer just wrote?" before a live run has to. Read-only.
    """
    import json as _json

    from .contracts import EnvelopeError, validate_envelope

    ov = _state_overrides(args)
    if args.watch:
        ov["watch_dir"] = args.watch
    if args.data:
        ov["data_dir"] = args.data
    cfg = load_config(env_file=args.env, **ov)
    path = Path(args.artifact) if args.artifact else _artifact_under(
        Path(cfg.watch_dir), recursive=cfg.watch_recursive
    )
    if path is None:
        print(f"probe: no research artifact under {cfg.watch_dir}", file=sys.stderr)
        return 1
    try:
        raw = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"probe: INVALID unreadable — {exc}", file=sys.stderr)
        return 1
    try:
        version = validate_envelope(raw, now=cfg.now())
    except EnvelopeError as exc:
        print(f"probe: INVALID {exc.reason_code} — {exc.detail}")
        return 1
    print(
        f"probe: OK {Path(path).name} schema={raw.get('schema_version')} "
        f"major={version[0]} ticker={raw.get('ticker')} expires_at={raw.get('expires_at')}"
    )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Full gate evaluation of a draft order - no side effects (plan §7.1 simulate)."""
    import json as _json

    from .risk.gate import GateContext, evaluate
    from .risk.ladder import rung_for
    from .risk.state import BookState, MarketState, RiskRequest
    from .risk.tail import ESResult
    from .risk.voltarget import vol_scalar
    from .sleeves.budgets import budget_for

    ov = _state_overrides(args)
    cfg = load_config(env_file=args.env, **ov)
    try:
        mandate = load_mandate(cfg.mandate_path)
    except MandateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stop_distance = abs(args.price - args.stop)
    request = RiskRequest(
        sleeve=args.sleeve,
        symbol=args.symbol.upper(),
        setup=args.setup,
        side=args.side,
        stop_distance=stop_distance,
        price=args.price,
        equity=args.equity,
        requested_risk_pct=args.risk,
        stop_price=args.stop,
        measured_move_bps=args.move_bps,
        round_trip_cost_bps=args.cost_bps,
        planned_notional_usd=args.notional,
        trailing_volume=args.adv,
    )
    book = BookState(equity=args.equity, cash=args.cash)
    market = MarketState(
        symbol=args.symbol.upper(),
        last=args.price,
        spread_bps=args.spread_bps,
        spread_median_bps=args.spread_bps,
        quote_age_s=0.0,
        bar_age_s=0.0,
        adv_shares=args.adv,
        atr=args.atr,
        day_type=args.day_type,
        data_quality=args.data_quality,
        price_caliber="adjusted",
        session="rth",
        feed=cfg.data_feed,
        shortable=True,
    )
    budget = budget_for(cfg, args.sleeve, book)
    stamp = cfg.now()
    if args.at:
        hour, _, minute = str(args.at).partition(":")
        stamp = stamp.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    ctx = GateContext(
        request=request,
        book=book,
        market=market,
        mandate=mandate,
        config=cfg,
        now=stamp,
        ladder=rung_for(
            day_pnl_pct=0.0,
            five_day_pct=0.0,
            drawdown_pct=0.0,
            soft_pct=cfg.daily_loss_soft_pct,
            hard_pct=cfg.daily_loss_hard_pct,
            derisk_5d_pct=cfg.derisk_5d_pct,
            derisk_dd_pct=cfg.derisk_dd_pct,
            halt_dd_pct=cfg.derisk_halt_dd_pct,
        ),
        vol=vol_scalar((), target=cfg.sleeve_intraday_vol_target, warmup_days=cfg.vol_warmup_days),
        es=(
            ESResult(
                value_pct=args.es_pct,
                estimator="historical",
                flagged=False,
                window_days=cfg.es_backtest_days,
                components={"historical": args.es_pct},
            )
            if args.es_pct is not None
            else ESResult(
                value_pct=0.0,
                estimator="unavailable",
                flagged=True,
                window_days=0,
                components={},
            )
        ),
        sleeve_ceiling_pct=budget.capital_ceiling_pct,
        sleeve_deployed_pct=0.0,
        setup_validated=True,
        day_type=args.day_type,
        stage="order",
    )
    decision = evaluate(ctx)
    print(
        _json.dumps(
            {
                "verdict": decision.verdict,
                "binding_gate": decision.binding_gate,
                "reason_code": decision.permission_reason_code,
                "adjusted_risk_pct": decision.adjusted_risk_pct,
                "approval_required": decision.approval_required,
                "reasons": list(decision.reasons),
                "state_snapshot": decision.state_snapshot,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    """Engage (or re-arm) the kill switch (plan §11.3). Re-arm needs a post-mortem."""
    ov = _state_overrides(args)
    if args.kill_switch:
        ov["kill_switch_path"] = args.kill_switch
    if args.data:
        ov["data_dir"] = args.data
    cfg = load_config(env_file=args.env, **ov)
    audit = AuditChain(cfg.audit_file, cfg.now)
    if args.resume:
        if not args.post_mortem:
            print(
                "error: re-arm requires --post-mortem <reference> (plan §11.3: manual only)",
                file=sys.stderr,
            )
            return 1
        resume(cfg.kill_switch_path, cfg.halt_latch_path)
        audit.append(
            "kill_switch_resumed",
            f"re-armed after {args.post_mortem}",
            post_mortem=args.post_mortem,
            operator=args.operator,
        )
        print(f"kill switch cleared; episode retained at {cfg.halt_latch_path}. "
              "Size restores in steps (25/50/100%) per plan §11.3.")
        return 0
    ep = halt_now(cfg, operator=args.operator, action="manual halt")
    print(f"HALTED episode {ep['episode']} since {ep['since']} (sentinel {cfg.kill_switch_path})")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    """Serve the signed control API on the loopback bind (plan §7.1, §12.4).

    Read + halt are wired; every order-path route answers 503 ``api_disabled``
    (no broker adapter yet). Refuses to start when the switch is off or the
    signing key is incomplete.
    """
    ov = _state_overrides(args)
    if args.bind:
        ov["api_bind"] = args.bind
    cfg = load_config(env_file=args.env, **ov)
    try:
        api = build_control_api(cfg)
    except ControlSurfaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"signald api {_now_str(cfg)} mode={cfg.mode} bind={cfg.api_bind} "
        f"key_id={cfg.api_key_id} (read+halt; order-path routes 503)",
        flush=True,
    )
    try:
        api.serve()
    except ValueError as exc:  # a non-loopback bind, refused before any socket
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the MCP surface (JSON-RPC over HTTP) on the loopback bind (plan §7.3).

    Read + halt + the proposal/approval stores are wired; a gate verdict and the
    order-path effects are not (no broker adapter yet), so those tools answer
    ``*_unavailable``. Refuses to start when the switch is off.
    """
    ov = _state_overrides(args)
    if args.bind:
        ov["mcp_bind"] = args.bind
    cfg = load_config(env_file=args.env, **ov)
    try:
        server = build_mcp_server(cfg)
    except ControlSurfaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"signald mcp {_now_str(cfg)} mode={cfg.mode} bind={cfg.mcp_bind} "
        f"toolsets={cfg.mcp_toolsets} (read+halt; gate/order effects unavailable)",
        flush=True,
    )
    try:
        server.serve(bind=cfg.mcp_bind)
    except ValueError as exc:  # a non-loopback bind, refused before any socket
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_scorecard(args: argparse.Namespace) -> int:
    """Summarise a trade-rows file into the committed scorecard (plan §10.3, §11.3)."""
    import json as _json

    from .lineage import (
        kill_shrink_decision,
        scorecard,
        write_review_report,
        write_scorecard,
    )

    cfg = load_config(env_file=args.env, **_state_overrides(args))
    path = Path(args.trades)
    if not path.exists():
        print(f"error: no trade rows at {path}", file=sys.stderr)
        return 1
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue
    sleeves = sorted({str(r.get("sleeve") or "unknown") for r in rows})
    if not sleeves:
        print("error: no usable trade rows", file=sys.stderr)
        return 1
    cards = {s: scorecard([r for r in rows if str(r.get("sleeve") or "unknown") == s], sleeve=s)
             for s in sleeves}
    out = write_scorecard(
        Path(args.out) if args.out else Path(cfg.data_dir) / "scorecard",
        scorecards=cards,
        comparison=None,
        trials=int(args.trials),
        generated_at=cfg.now(),
    )
    for s, card in cards.items():
        print(f"{s}: trades={card.trades} adequate={card.sample_adequate}")
    print(f"wrote scorecard: {out}")
    if args.review:
        decisions = []
        for s, card in sorted(cards.items()):
            metrics = card.metrics
            expectancy = metrics.get("expectancy_usd")
            decisions.append(
                kill_shrink_decision(
                    sleeve=s,
                    trades=card.trades,
                    expectancy_usd=None if expectancy is None else float(expectancy),
                    current_pct=0.0,
                    sample_adequate=card.sample_adequate,
                    cvar_share=metrics.get("cvar_share"),
                    return_share=metrics.get("return_share"),
                    shrink_pct=0.10,
                )
            )
        review = write_review_report(
            Path(args.review), decisions=decisions, generated_at=cfg.now(), trials=int(args.trials)
        )
        for d in decisions:
            print(f"review {d.sleeve}: {d.verdict} ({d.reason_code})")
        print(f"wrote review: {review}")
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
    v.add_argument("--audit", default=None,
                   help="audit file path (default: the configured ledger)")
    v.add_argument("--env", help=".env file path")
    v.add_argument("--data", help="signals output directory")
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

    ma = sub.add_parser("mandate-add", help="add a symbol to the signed mandate (re-signs)")
    ma.add_argument("symbol")
    ma.add_argument("--operator", default="vince")
    ma.add_argument("--reason", default="", help="why (recorded in the archive row)")
    ma.add_argument("--mandate", default=None, help="mandate JSON path (overrides env/default)")
    ma.add_argument("--env")
    ma.add_argument("--data", help="signals output directory")
    ma.set_defaults(func=cmd_mandate_add)

    mr = sub.add_parser("mandate-remove", help="remove a symbol from the signed mandate (re-signs)")
    mr.add_argument("symbol")
    mr.add_argument("--operator", default="vince")
    mr.add_argument("--reason", default="", help="why (recorded in the archive row)")
    mr.add_argument(
        "--force",
        action="store_true",
        help="remove even when the symbol is held or holdings cannot be verified",
    )
    mr.add_argument("--mandate", default=None, help="mandate JSON path (overrides env/default)")
    mr.add_argument("--env")
    mr.add_argument("--data", help="signals output directory")
    mr.set_defaults(func=cmd_mandate_remove)

    pr = sub.add_parser("probe", help="validate the newest research artifact (read-only)")
    pr.add_argument("--artifact", help="explicit artifact path")
    pr.add_argument("--watch")
    pr.add_argument("--data")
    pr.add_argument("--env")
    pr.set_defaults(func=cmd_probe)

    si = sub.add_parser("simulate", help="full gate evaluation of a draft order (no side effects)")
    si.add_argument("--symbol", required=True)
    si.add_argument("--setup", default="ORB_RVOL")
    si.add_argument("--side", choices=["buy", "sell"], default="buy")
    si.add_argument("--sleeve", choices=["swing", "intraday"], default="intraday")
    si.add_argument("--price", type=float, required=True)
    si.add_argument("--stop", type=float, required=True)
    si.add_argument("--risk", type=float, default=0.0025, help="risk fraction of equity")
    si.add_argument("--equity", type=float, default=100000.0)
    si.add_argument("--cash", type=float, default=50000.0)
    si.add_argument("--adv", type=float, default=5000000.0)
    si.add_argument("--atr", type=float, default=1.0)
    si.add_argument("--spread-bps", type=float, default=4.0, dest="spread_bps")
    si.add_argument("--move-bps", type=float, default=60.0, dest="move_bps")
    si.add_argument("--cost-bps", type=float, default=None, dest="cost_bps")
    si.add_argument("--notional", type=float, default=None)
    si.add_argument(
        "--es-pct",
        type=float,
        default=None,
        dest="es_pct",
        help="the book's measured 1-day ES as a fraction (omitting it blocks on house_cvar)",
    )
    si.add_argument(
        "--at",
        default=None,
        help="simulate at HH:MM instead of now (the intraday window is time-gated)",
    )
    si.add_argument("--day-type", default="trend", dest="day_type")
    si.add_argument("--data-quality", default="fresh", dest="data_quality")
    si.add_argument("--env")
    si.add_argument("--data", dest="data")
    si.set_defaults(func=cmd_simulate)

    api = sub.add_parser("api", help="serve the signed control API (loopback only)")
    api.add_argument("--bind", help="host:port (default TRADINGEXEC_API_BIND)")
    api.add_argument("--env")
    api.add_argument("--data")
    api.set_defaults(func=cmd_api)

    mcp = sub.add_parser("mcp", help="serve the MCP surface over JSON-RPC/HTTP (loopback only)")
    mcp.add_argument("--bind", help="host[:port] (default TRADINGEXEC_MCP_BIND)")
    mcp.add_argument("--env")
    mcp.add_argument("--data")
    mcp.set_defaults(func=cmd_mcp)

    ha = sub.add_parser("halt", help="engage the kill switch; --resume re-arms it")
    ha.add_argument(
        "--resume", action="store_true", help="clear the sentinel (needs --post-mortem)"
    )
    ha.add_argument("--post-mortem", default=None)
    ha.add_argument("--operator", default="vince")
    ha.add_argument("--kill-switch", default=None)
    ha.add_argument("--data")
    ha.add_argument("--env")
    ha.set_defaults(func=cmd_halt)

    sc = sub.add_parser("scorecard", help="write the sleeve scorecard from a trade-rows JSONL")
    sc.add_argument("--trades", required=True, help="trade rows JSONL (plan §10.2 shape)")
    sc.add_argument("--out", default=None, help="output basename (default <data>/scorecard)")
    sc.add_argument("--trials", type=int, default=0)
    sc.add_argument(
        "--review", default=None, help="also write the kill/shrink review note at this basename"
    )
    sc.add_argument("--env")
    sc.add_argument("--data")
    sc.set_defaults(func=cmd_scorecard)

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
