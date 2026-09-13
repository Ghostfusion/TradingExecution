"""The v2 CLI surface: probe, simulate, halt/re-arm, scorecard.

These are the operator commands the plan's runbook names, so each test drives the
real `main()` with an isolated `--data` tree and no network.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from signald import cli
from signald.samples import build_sample, build_sample_v11

pytestmark = pytest.mark.timeout(120)


def _env(tmp_path) -> str:
    """A .env that points EVERY path under tmp_path (never the developer's tree).

    Every state path the CLI can write is redirected here - a missed one writes
    into the repo's operational `audit/` directory.
    """
    env = tmp_path / ".env"
    env.write_text(
        f"TRADINGEXEC_DATA_DIR={tmp_path / 'signals'}\n"
        f"TRADINGEXEC_WATCH_DIR={tmp_path / 'decisions'}\n"
        f"TRADINGEXEC_MANDATE_PATH={tmp_path / 'mandate.json'}\n"
        f"TRADINGEXEC_KILL_SWITCH_PATH={tmp_path / 'kill_switch'}\n"
        f"TRADINGEXEC_AUDIT_FILE={tmp_path / 'audit' / 'audit.jsonl'}\n"
        f"TRADINGEXEC_JOURNAL_FILE={tmp_path / 'audit' / 'journal.jsonl'}\n"
        f"TRADINGEXEC_HALT_LATCH_PATH={tmp_path / 'audit' / 'halt_episode.json'}\n"
        f"TRADINGEXEC_HEARTBEAT_PATH={tmp_path / 'audit' / 'heartbeat'}\n"
        f"TRADINGEXEC_PID_FILE={tmp_path / 'signald.pid'}\n",
        encoding="utf-8",
    )
    return str(env)


@pytest.fixture()
def mandate_file(tmp_path):
    """A signed mandate under tmp_path (simulate refuses without one)."""
    from signald.mandate import DEFAULT_MANDATE, write_mandate

    return write_mandate(tmp_path / "mandate.json", DEFAULT_MANDATE)


def _drop(tmp_path, doc: dict, name: str = "research_decision.json"):
    path = tmp_path / "decisions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _v11(**kw):
    now = datetime.now(UTC)
    kw.setdefault("effective_date", date.today())
    kw.setdefault("produced_at", now - timedelta(hours=1))
    kw.setdefault("expires_at", now + timedelta(hours=6))
    return build_sample_v11(**kw)


# --- probe -----------------------------------------------------------------
def test_probe_accepts_a_valid_v1_1_artifact(tmp_path, capsys):
    _drop(tmp_path, _v11())

    rc = cli.main(["probe", "--env", _env(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("probe: OK")
    assert "schema=1.1.0" in out and "ticker=AVGO" in out


def test_probe_rejects_an_unknown_major(tmp_path, capsys):
    _drop(tmp_path, _v11(schema_version="2.0.0"))

    rc = cli.main(["probe", "--env", _env(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 1 and "INVALID unknown_major" in out


def test_probe_reports_an_empty_inbox(tmp_path, capsys):
    rc = cli.main(["probe", "--env", _env(tmp_path)])

    assert rc == 1 and "no research artifact" in capsys.readouterr().err


def test_probe_accepts_a_legacy_artifact(tmp_path, capsys):
    """Back-compat check: a version-less Phase-A artifact is still ingestible."""
    _drop(tmp_path, build_sample(effective_date=date.today()))

    rc = cli.main(["probe", "--env", _env(tmp_path)])

    assert rc == 0 and "probe: OK" in capsys.readouterr().out


# --- simulate --------------------------------------------------------------
def _sim_args(tmp_path, *extra: str) -> list[str]:
    return [
        "simulate",
        "--symbol", "AVGO",
        "--price", "100",
        "--stop", "95",
        "--adv", "5000000",
        "--spread-bps", "4",
        "--move-bps", "60",
        "--cost-bps", "12",
        "--equity", "100000",
        "--es-pct", "0.004",
        "--at", "09:45",
        "--env", _env(tmp_path),
        *extra,
    ]


def test_simulate_allows_a_clean_order_and_writes_nothing(tmp_path, capsys, mandate_file):
    args = _sim_args(tmp_path, "--data", str(tmp_path / "signals"))

    rc = cli.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] in {"ALLOW", "REDUCE"}
    assert payload["state_snapshot"]["equity"] == 100_000.0
    assert not (tmp_path / "signals").exists(), "simulate must have no side effects"


def test_simulate_names_the_binding_gate_when_it_refuses(tmp_path, capsys, mandate_file):
    args = _sim_args(tmp_path, "--move-bps", "10")  # 10 bps <= 3 x 12 bps

    rc = cli.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "BLOCK"
    assert payload["binding_gate"] == "cost"
    assert payload["reason_code"] == "cost_gate"


def test_simulate_needs_a_mandate(tmp_path, capsys):
    rc = cli.main(_sim_args(tmp_path))

    assert rc == 1 and "mandate" in capsys.readouterr().err.lower()


def test_simulate_outside_the_entry_window_is_time_gated(tmp_path, capsys, mandate_file):
    args = _sim_args(tmp_path, "--at", "20:00")

    cli.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["binding_gate"] == "time"


def test_simulate_without_an_es_estimate_blocks_and_says_why(tmp_path, capsys, mandate_file):
    """Fail closed: an unmeasurable CVaR budget is not a passing CVaR budget."""
    args = [a for a in _sim_args(tmp_path) if a not in ("--es-pct", "0.004")]

    rc = cli.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "BLOCK"
    assert payload["binding_gate"] == "house_cvar"
    assert payload["reason_code"] == "es_unavailable"


# --- halt / re-arm ---------------------------------------------------------
def test_halt_engages_the_kill_switch_and_latches_an_episode(tmp_path, capsys):
    env = _env(tmp_path)

    rc = cli.main(["halt", "--operator", "vince", "--env", env])

    out = capsys.readouterr().out
    assert rc == 0 and "HALTED episode" in out
    assert (tmp_path / "kill_switch").exists()
    latch = json.loads((tmp_path / "audit" / "halt_episode.json").read_text(encoding="utf-8"))
    assert latch["episode"] and latch["reason"]


def test_status_reports_the_halt(tmp_path, capsys):
    cli.main(["halt", "--env", _env(tmp_path)])
    capsys.readouterr()

    cli.main(["status", "--env", _env(tmp_path)])

    assert "kill_switch: HALTED" in capsys.readouterr().out


def test_re_arm_without_a_post_mortem_is_refused(tmp_path, capsys):
    env = _env(tmp_path)
    cli.main(["halt", "--env", env])
    capsys.readouterr()

    rc = cli.main(["halt", "--resume", "--env", env])

    assert rc == 1 and "post-mortem" in capsys.readouterr().err
    assert (tmp_path / "kill_switch").exists(), "a refused re-arm must not clear the sentinel"


def test_re_arm_with_a_post_mortem_clears_the_sentinel_and_keeps_the_episode(tmp_path, capsys):
    env = _env(tmp_path)
    cli.main(["halt", "--env", env])
    episode = json.loads(
        (tmp_path / "audit" / "halt_episode.json").read_text(encoding="utf-8")
    )["episode"]
    capsys.readouterr()

    rc = cli.main(
        ["halt", "--resume", "--post-mortem", "PM-2026-09-12", "--env", env]
    )

    out = capsys.readouterr().out
    assert rc == 0 and "25/50/100%" in out
    assert not (tmp_path / "kill_switch").exists()
    kept = json.loads((tmp_path / "audit" / "halt_episode.json").read_text(encoding="utf-8"))
    assert kept["episode"] == episode
    audit = (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8")
    assert "kill_switch_resumed" in audit and "PM-2026-09-12" in audit


# --- scorecard -------------------------------------------------------------
def test_scorecard_writes_json_and_markdown(tmp_path, capsys):
    rows = tmp_path / "trades.jsonl"
    rows.write_text(
        "\n".join(
            json.dumps(
                {
                    "sleeve": "intraday",
                    "symbol": "AVGO",
                    "setup": "ORB_RVOL",
                    "entry_ts": "2026-09-12T09:40:00",
                    "exit_ts": "2026-09-12T10:10:00",
                    "qty": 10,
                    "entry_px": 100.0,
                    "exit_px": 101.0,
                    "slippage_bps": 2.0,
                    "fees_usd": 0.5,
                    "r_multiple": 1.8,
                }
            )
            for _ in range(3)
        ),
        encoding="utf-8",
    )

    rc = cli.main(
        ["scorecard", "--trades", str(rows), "--env", _env(tmp_path),
         "--out", str(tmp_path / "card"), "--trials", "5"]
    )

    out = capsys.readouterr().out
    assert rc == 0 and "intraday: trades=3" in out
    assert (tmp_path / "card.json").exists() and (tmp_path / "card.md").exists()
    markdown = (tmp_path / "card.md").read_text(encoding="utf-8")
    assert "Trials (N): 5" in markdown  # the trial count is a first-class output
    assert "feasibility, not superiority" in markdown  # no winner is declared


def test_scorecard_can_write_the_kill_shrink_review(tmp_path, capsys):
    rows = tmp_path / "trades.jsonl"
    rows.write_text(
        "\n".join(
            json.dumps(
                {
                    "sleeve": "intraday",
                    "symbol": "AVGO",
                    "qty": 10,
                    "entry_px": 100.0,
                    "exit_px": 99.0,
                    "fees_usd": 0.5,
                    "r_multiple": -1.0,
                }
            )
            for _ in range(3)
        ),
        encoding="utf-8",
    )

    rc = cli.main(
        ["scorecard", "--trades", str(rows), "--env", _env(tmp_path),
         "--out", str(tmp_path / "card"), "--review", str(tmp_path / "review"), "--trials", "3"]
    )

    out = capsys.readouterr().out
    assert rc == 0 and "review intraday:" in out
    note = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "Trials (N): 3" in note and "**keep**" in note  # three trades cannot kill


def test_scorecard_reports_a_missing_file(tmp_path, capsys):
    rc = cli.main(["scorecard", "--trades", str(tmp_path / "nope.jsonl"), "--env", _env(tmp_path)])

    assert rc == 1 and "no trade rows" in capsys.readouterr().err
