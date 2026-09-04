"""Discord formatter tests (hermetic; no network)."""

from __future__ import annotations

import pytest

from signald.notifier import Notifier

pytestmark = pytest.mark.timeout(120)


def _signal_envelope():
    return {
        "signal_id": "sg-abc-001",
        "decision_hash": "sha256:abc",
        "ticker": "AVGO",
        "action": "REDUCE",
        "target_pct": 0.0,
        "stop": 429.0,
        "take_profit": None,
        "expiry": "2027-01-01",
        "ref": {"last": 355.6},
        "expected_cost_band_bps": [3, 8],
        "gates": {"verdict": "DOWNGRADE"},
        "emitted_at": "2026-09-03T20:00:00",
        "config_hash": "hash",
    }


def test_signal_formats_discord_card():
    n = Notifier()
    ev = n.signal_event(_signal_envelope())
    d = n.discord_event(ev)
    assert d["content"].startswith("Signal sg-abc-001")
    assert "AVGO" in d["content"] and "REDUCE" in d["content"]
    assert "355.6" in d["content"]
    embeds = d["embeds"][0]
    labels = {f["name"] for f in embeds["fields"]}
    assert {"target %", "stop"} <= labels
    assert embeds["title"] == "AVGO signal"


def test_non_signal_event_plain_line():
    n = Notifier()
    d = n.discord_event({"event": "heartbeat_loss", "detail": "stale 300s"})
    assert d["content"] == "heartbeat_loss: stale 300s"
    assert d.get("embeds") is None or d["embeds"] == []


def test_error_event_plain_line():
    n = Notifier()
    d = n.discord_event({"event": "error", "detail": "reference unavailable"})
    assert d["content"] == "error: reference unavailable"
