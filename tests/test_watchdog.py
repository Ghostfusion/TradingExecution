"""Watchdog + notifier tests (hermetic; no network)."""

from __future__ import annotations

import pytest

from signald.notifier import Notifier
from signald.watchdog import check_heartbeat, heartbeat_age_seconds

pytestmark = pytest.mark.timeout(120)

NOW_S = 1_750_000_000.0


def _notifier(events):
    now = lambda: __import__("datetime").datetime(2026, 9, 3)  # noqa: E731
    return Notifier(transport=lambda e: events.append(e), now=now)


def test_fresh_heartbeat_passes(tmp_path):
    hp = tmp_path / "heartbeat"
    hp.write_text(str(NOW_S - 10), encoding="utf-8")
    assert check_heartbeat(hp, _notifier([]), max_age_s=120, now=NOW_S) is True


def test_stale_heartbeat_dispatches_loss(tmp_path):
    hp = tmp_path / "heartbeat"
    hp.write_text(str(NOW_S - 500), encoding="utf-8")
    events = []
    assert check_heartbeat(hp, _notifier(events), max_age_s=120, now=NOW_S) is False
    assert events and events[0]["event"] == "heartbeat_loss"
    assert "stale" in events[0]["detail"]


def test_missing_heartbeat_dispatches(tmp_path):
    events = []
    assert check_heartbeat(tmp_path / "nope", _notifier(events), max_age_s=120, now=NOW_S) is False
    assert events and events[0]["event"] == "heartbeat_loss"
    assert "missing" in events[0]["detail"]


def test_age_helper(tmp_path):
    hp = tmp_path / "heartbeat"
    hp.write_text(str(NOW_S - 7), encoding="utf-8")
    assert heartbeat_age_seconds(hp, now=NOW_S) == 7.0
    assert heartbeat_age_seconds(tmp_path / "missing", now=NOW_S) is None


def test_processor_dispatches_error_event(processor, write_artifact, seam, webhook_events):
    seam.state["account"]["cash"] = 1000.0  # below reserve -> block -> notifier error event
    from signald.samples import build_sample

    processor.process(write_artifact(build_sample()))
    kinds = [e["event"] for e in webhook_events]
    assert "error" in kinds


def test_notify_test_cli_no_url_fails(tmp_path, capsys):
    from signald import cli

    cfg = tmp_path / ".env"
    cfg.write_text("", encoding="utf-8")
    rc = cli.main(["notify-test", "--env", str(cfg)])
    assert rc == 1
    assert "not configured" in capsys.readouterr().err


def test_watchdog_cli_stale(tmp_path):
    import time as _t

    from signald import cli

    hp = tmp_path / "heartbeat"
    hp.write_text(str(_t.time() - 999), encoding="utf-8")
    assert cli.main(["watchdog", "--heartbeat", str(hp), "--max-age", "120",
                     "--env", str(tmp_path / "none.env")]) == 1
    hp.write_text(str(_t.time() - 1), encoding="utf-8")
    assert cli.main(["watchdog", "--heartbeat", str(hp), "--max-age", "120",
                     "--env", str(tmp_path / "none.env")]) == 0
