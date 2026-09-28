"""The hold ledger (owner instruction 2026-09-28): every HOLD is recorded.

The rule under test is the one that used to erase information. A ``hold`` on a
name the mandate bars was REFUSED and then dropped: no signal (correctly - it is
not tradable), no queue entry (``PROMOTABLE_RATINGS`` is buy/overweight only),
and nothing durable that said research had said "hold". An in-mandate hold, by
contrast, emitted a HOLD signal and was visible.

These tests pin the three halves of the instruction: a hold lands in the ledger
**whether or not the symbol is in the mandate**, it stays **untradable** either
way, and only the previously-silent case pages the operator (the in-mandate hold
already dispatches its own HOLD signal, so paging here too would double it).
"""

from __future__ import annotations

import pytest

from signald.samples import build_sample_v11
from signald.stores import AuditChain, MonitorStore

pytestmark = pytest.mark.timeout(120)

#: In ``DEFAULT_MANDATE`` (and the sample builder's own default).
IN_MANDATE = "AVGO"
#: Deliberately NOT in ``DEFAULT_MANDATE`` - the case this ledger exists for.
OUTSIDE = "NFLX"


def _monitors(cfg) -> list[dict]:
    return MonitorStore(cfg.data_dir / "monitor.jsonl").read_all()


# --------------------------------------------------------------------------
# The store: append-only, keyed on (ticker, decision_hash)
# --------------------------------------------------------------------------
def test_the_store_is_keyed_on_ticker_and_decision_hash(tmp_path):
    """`has` is the net the processor leans on; a different artifact is a new row."""
    store = MonitorStore(tmp_path / "monitor.jsonl")
    assert store.has("NFLX", "abc") is False

    store.record(ticker="nflx", rating="hold", action="HOLD", decision_hash="abc",
                 in_mandate=False, blocked=True, at="2026-09-28T12:00:00")

    assert store.has("NFLX", "abc") is True           # case-folded
    assert store.has("NFLX", "different-hash") is False
    rows = store.read_all()
    assert len(rows) == 1
    assert rows[0]["type"] == "monitor" and rows[0]["ticker"] == "NFLX"
    assert rows[0]["blocked"] is True and rows[0]["in_mandate"] is False


# --------------------------------------------------------------------------
# The instruction: recorded regardless of mandate membership
# --------------------------------------------------------------------------
def test_a_hold_outside_the_mandate_is_recorded(processor, write_artifact, cfg):
    """THE case: refused by the gate, and still recorded as a hold.

    Before this ledger an out-of-mandate hold produced a rejected audit row and
    an error card, and nothing that kept the hold itself.
    """
    p = write_artifact(build_sample_v11(ticker=OUTSIDE, direction="hold"))

    res = processor.process(p)

    assert res.kind == "blocked", res.reasons
    assert res.envelope is None, "a blocked hold must not become a signal"
    rows = _monitors(cfg)
    assert len(rows) == 1
    assert rows[0]["ticker"] == OUTSIDE
    assert rows[0]["action"] == "HOLD"
    assert rows[0]["blocked"] is True
    assert rows[0]["in_mandate"] is False


def test_a_hold_inside_the_mandate_is_recorded_too(processor, write_artifact, cfg):
    """`regardless if the symbol is in the mandate` - the in-mandate hold as well.

    It already emits a HOLD signal; recording it here is what makes the two
    cases comparable instead of one being visible and the other invisible.
    """
    p = write_artifact(build_sample_v11(ticker=IN_MANDATE, direction="hold"))

    res = processor.process(p)

    assert res.kind == "emitted", res.reasons
    assert res.envelope["action"] == "HOLD"
    rows = _monitors(cfg)
    assert len(rows) == 1
    assert rows[0]["ticker"] == IN_MANDATE
    assert rows[0]["blocked"] is False
    assert rows[0]["in_mandate"] is True


def test_only_a_hold_is_monitored(processor, write_artifact, cfg):
    """A reduce is not a hold: this stays the hold ledger, not a second signal log."""
    p = write_artifact(build_sample_v11(ticker=IN_MANDATE, direction="reduce"))

    assert processor.process(p).kind == "emitted"

    assert _monitors(cfg) == []


def test_the_ledger_is_appended_once_per_decision(processor, write_artifact, cfg):
    """The 10 s poll re-discovers the artifact; the ledger records the DECISION once.

    2026-09-16: re-processing one unchanged artifact put ~26k identical lines a
    day on stdout. The journal already dedupes by decision_hash, so the store's
    own dedupe is exercised with that first line of defence disabled.
    """
    p = write_artifact(build_sample_v11(ticker=OUTSIDE, direction="hold"))
    assert processor.process(p).kind == "blocked"

    processor.journal.mark_processed = lambda *a, **k: None  # bypass the journal
    processor.process(p)

    assert len(_monitors(cfg)) == 1


# --------------------------------------------------------------------------
# Notification: page the case that used to be silent, and only that one
# --------------------------------------------------------------------------
def test_only_the_out_of_mandate_hold_pages(processor, write_artifact, webhook_events):
    """An in-mandate hold already sends its HOLD signal; paging too would double it."""
    processor.process(
        write_artifact(build_sample_v11(ticker=IN_MANDATE, direction="hold"))
    )
    assert [e["event"] for e in webhook_events] == ["signal"]

    webhook_events.clear()
    processor.process(
        write_artifact(build_sample_v11(ticker=OUTSIDE, direction="hold"))
    )

    kinds = [e["event"] for e in webhook_events]
    assert "monitor" in kinds, kinds
    assert "signal" not in kinds, "a blocked hold must never dispatch a signal"
    ev = next(e for e in webhook_events if e["event"] == "monitor")
    assert ev["ticker"] == OUTSIDE and ev["action"] == "HOLD"
    # The refusal is still announced as it was before the ledger existed.
    assert "error" in kinds
    # A hold is not a promotion request: the candidate queue's command is absent.
    assert "command" not in ev


def test_an_in_mandate_hold_blocked_on_another_gate_is_recorded_truthfully(
    processor, write_artifact, cfg, transport_state, webhook_events
):
    """`blocked` is NOT `outside the mandate` - the sentence must say which.

    Measured live 2026-09-28 against the real mandate and the sample artifacts:
    NVDA (in the mandate) was blocked on the ``data`` gate and the audit row read
    "outside the mandate", which is false. The structured ``in_mandate`` field was
    right; the sentence was not - and the sentence is what an operator reads.

    Here the symbol IS allowed and the decision is still blocked (the account is
    under the mandate's cash floor, which is a mandate-level gate), so
    ``blocked=True`` and ``in_mandate=True`` - the pair the wording has to keep
    apart.
    """
    transport_state["account"]["cash"] = 1000.0  # below the 25k reserve
    p = write_artifact(build_sample_v11(ticker=IN_MANDATE, direction="hold"))

    res = processor.process(p)

    assert res.kind == "blocked", res.reasons
    rows = _monitors(cfg)
    assert len(rows) == 1
    assert rows[0]["in_mandate"] is True, "the symbol IS allowed"
    assert rows[0]["blocked"] is True, "and the decision is still refused"
    reasons = [r["reason"] for r in AuditChain(cfg.audit_file, cfg.now).read()
               if r["kind"] == "monitor"]
    assert reasons, "the hold was not audited"
    assert "inside the mandate" in reasons[0], reasons[0]
    assert "outside" not in reasons[0], reasons[0]
    # The page belongs to the mandate-barred case only; this one already has its
    # own error card, so a monitor card here would be the duplicate.
    assert "monitor" not in [e["event"] for e in webhook_events]


# --------------------------------------------------------------------------
# The status read-out: counting and wording key on in_mandate, not blocked
# --------------------------------------------------------------------------
def test_the_status_line_does_not_call_a_blocked_in_mandate_hold_outside():
    """The same conflation the audit sentence had, in the operator's read-out.

    2026-09-28 live smoke: NVDA (``in_mandate=True``, blocked on ``data``) was
    counted inside "outside the mandate: 2" and printed with the wrong side.
    """
    from signald.cli import _monitor_summary

    rows = [
        {"ticker": "NFLX", "rating": "hold", "in_mandate": False, "blocked": True,
         "at": "2026-09-28T12:00:00"},
        {"ticker": "NVDA", "rating": "hold", "in_mandate": True, "blocked": True,
         "at": "2026-09-28T12:00:01"},
        {"ticker": "GOOG", "rating": "hold", "in_mandate": True, "blocked": False,
         "at": "2026-09-28T12:00:02"},
    ]

    lines = _monitor_summary(rows)

    assert "outside the mandate: 1" in lines[0], lines[0]
    assert "NFLX" in lines[1] and "outside the mandate" in lines[1]
    assert "NVDA" in lines[2] and "inside the mandate" in lines[2]
    assert "GOOG" in lines[3] and "inside the mandate" in lines[3]
    assert "(blocked)" in lines[2], "a refused hold should say so"
    assert "(blocked)" not in lines[3]


def test_the_status_read_out_says_zero_when_the_ledger_is_empty():
    from signald.cli import _monitor_summary

    assert _monitor_summary([]) == ["monitors (holds): 0"]


def test_the_monitor_event_renders_a_real_card(notifier):
    """A new event kind must not fall through to the empty generic line."""
    ev = notifier.monitor_event(ticker="NFLX", rating="hold",
                                decision_hash="d" * 8, binding_gate="mandate")
    card = notifier.discord_event(ev)

    assert "NFLX" in card["content"] and "HOLD" in card["content"]
    assert card["content"].strip() != "monitor:"
    assert card["embeds"][0]["fields"], "the card lost its fields"
