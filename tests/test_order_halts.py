"""LULD/halt state machine tests (plan §5.3, design §6.4/§14).

Every phase transition, the entry block while halted and through the cooldown,
the 300 s cooldown decay on an injected clock, the last-10-minutes no-reopen
rule, idempotent updates, and the marketable-exit block during a halt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from signald.order.halts import (
    DEFAULT_COOLDOWN_S,
    NO_REOPEN_MINUTES,
    PHASES,
    HaltMachine,
    HaltState,
)

pytestmark = pytest.mark.timeout(120)


def _machine(at: datetime, **kwargs) -> tuple[HaltMachine, list[datetime]]:
    """A machine over a mutable one-slot clock (no wall clock in any test)."""
    box = [at]
    return HaltMachine(lambda: box[0], **kwargs), box


def test_phases_is_the_closed_set():
    assert PHASES == ("normal", "limit_state", "halted", "reopening", "cooldown")
    assert NO_REOPEN_MINUTES == 10.0
    assert DEFAULT_COOLDOWN_S == 300.0


def test_a_fresh_machine_is_normal_and_allows_both(now):
    machine, _ = _machine(now)
    state = machine.state()
    assert isinstance(state, HaltState)
    assert state.phase == "normal"
    assert state.allow_entries is True
    assert state.allow_exits is True
    assert state.cooldown_s == 0.0
    assert state.reason == "normal"
    assert state.since is None


def test_halt_blocks_entries_and_marketable_exits(now):
    machine, _ = _machine(now)
    state = machine.update(halted=True)
    assert state.phase == "halted"
    assert state.allow_entries is False
    assert state.allow_exits is False
    assert state.reason == "halt"
    assert state.since == now.isoformat()


def test_limit_state_blocks_entries_and_marketable_exits(now):
    machine, _ = _machine(now)
    state = machine.update(halted=False, limit_state=True)
    assert state.phase == "limit_state"
    assert state.allow_entries is False
    assert state.allow_exits is False
    assert state.reason == "limit_state"


def test_a_full_halt_dominates_a_limit_state(now):
    machine, _ = _machine(now)
    state = machine.update(halted=True, limit_state=True)
    assert state.phase == "halted"
    assert state.reason == "halt"


def test_clearing_a_halt_enters_reopening_not_normal(now):
    machine, _ = _machine(now)
    machine.update(halted=True)
    state = machine.update(halted=False)
    assert state.phase == "reopening"
    assert state.allow_entries is False
    # Never a marketable order into a reopening auction.
    assert state.allow_exits is False
    assert state.reason == "reopening"


def test_clearing_a_limit_state_also_enters_reopening(now):
    machine, _ = _machine(now)
    machine.update(halted=False, limit_state=True)
    state = machine.update(halted=False)
    assert state.phase == "reopening"
    assert state.allow_entries is False


def test_reopening_does_not_self_clear_without_confirmation(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    box[0] = now + timedelta(seconds=3600)
    state = machine.update(halted=False)
    # An unconfirmed reopen never silently becomes normal.
    assert state.phase == "reopening"
    assert state.allow_entries is False


def test_note_reopen_starts_the_cooldown(now):
    machine, _ = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    state = machine.state()
    assert state.phase == "cooldown"
    assert state.allow_entries is False
    assert state.allow_exits is True  # the market is open again; only entries wait
    assert state.reason == "cooldown"
    assert state.cooldown_s == 0.0  # the cooldown has just started


def test_cooldown_decays_on_the_injected_clock(now):
    # Hand-worked: reopen at T, configured cooldown 300 s; at T+120 the cooldown
    # has been running 120 s, still below the 300 s gate threshold.
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=120)
    state = machine.update(halted=False)
    assert state.phase == "cooldown"
    assert state.cooldown_s == 120.0
    assert state.cooldown_s < DEFAULT_COOLDOWN_S  # the gate's reopen-cooldown block
    assert state.allow_entries is False


def test_cooldown_clears_after_300s(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=DEFAULT_COOLDOWN_S)
    state = machine.update(halted=False)
    assert state.phase == "normal"
    assert state.allow_entries is True
    assert state.allow_exits is True
    assert state.cooldown_s == 0.0


def test_cooldown_boundary_is_inclusive(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=DEFAULT_COOLDOWN_S - 0.1)
    assert machine.update(halted=False).phase == "cooldown"
    box[0] = now + timedelta(seconds=DEFAULT_COOLDOWN_S)
    assert machine.update(halted=False).phase == "normal"


def test_state_reads_the_clock_without_an_update(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=90)
    assert machine.state().cooldown_s == 90.0


@pytest.mark.parametrize(
    ("halted", "limit_state"),
    [
        (True, False),
        (False, True),
        (True, True),
        (False, False),
    ],
)
def test_update_is_idempotent_for_the_same_inputs(now, halted, limit_state):
    machine, _ = _machine(now)
    first = machine.update(halted=halted, limit_state=limit_state)
    second = machine.update(halted=halted, limit_state=limit_state)
    assert first == second


def test_idempotent_across_the_whole_cycle(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=120)
    first = machine.update(halted=False)
    second = machine.update(halted=False)
    assert first == second
    assert first.cooldown_s == 120.0


def test_a_halt_in_the_last_10_minutes_never_reopens(now):
    machine, _ = _machine(now)
    started = machine.update(halted=True, minutes_to_close=NO_REOPEN_MINUTES)
    assert started.phase == "halted"
    assert started.reason == "no_reopen_last_10m"
    cleared = machine.update(halted=False, minutes_to_close=5.0)
    assert cleared.phase == "halted"
    assert cleared.reason == "no_reopen_last_10m"
    assert cleared.allow_entries is False
    assert machine.update(halted=False).phase == "halted"


def test_note_reopen_is_ignored_in_the_never_reopen_episode(now):
    machine, box = _machine(now)
    machine.update(halted=True, minutes_to_close=2.0)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=600)
    state = machine.update(halted=False)
    assert state.phase == "halted"
    assert state.reason == "no_reopen_last_10m"


def test_a_limit_state_after_the_never_reopen_halt_stays_halted(now):
    machine, _ = _machine(now)
    machine.update(halted=True, minutes_to_close=1.0)
    state = machine.update(halted=False, limit_state=True)
    assert state.phase == "halted"
    assert state.reason == "no_reopen_last_10m"
    assert state.allow_entries is False


def test_a_halt_before_the_last_10_minutes_reopens_normally(now):
    machine, _ = _machine(now)
    started = machine.update(halted=True, minutes_to_close=30.0)
    assert started.reason == "halt"
    machine.update(halted=False)
    machine.note_reopen()
    assert machine.state().phase == "cooldown"


def test_unknown_minutes_to_close_does_not_block_a_confirmed_reopen(now):
    machine, _ = _machine(now)
    machine.update(halted=True)
    assert machine.update(halted=False).phase == "reopening"
    machine.note_reopen()
    assert machine.state().phase == "cooldown"


def test_note_reopen_before_a_clear_is_ignored(now):
    machine, _ = _machine(now)
    machine.update(halted=True)
    machine.note_reopen()
    assert machine.state().phase == "halted"


def test_rehalt_during_the_cooldown_restarts_the_episode(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now + timedelta(seconds=60)
    again = machine.update(halted=True)
    assert again.phase == "halted"
    assert again.allow_entries is False
    assert again.allow_exits is False
    box[0] = now + timedelta(seconds=61)
    assert machine.update(halted=False).phase == "reopening"
    machine.note_reopen()
    assert machine.state().phase == "cooldown"


def test_the_whole_cycle_visits_every_phase(now):
    machine, box = _machine(now)
    seen = [machine.state().phase]
    seen.append(machine.update(halted=True).phase)
    seen.append(machine.update(halted=False).phase)
    machine.note_reopen()
    seen.append(machine.state().phase)
    box[0] = now + timedelta(seconds=DEFAULT_COOLDOWN_S)
    seen.append(machine.update(halted=False).phase)
    assert tuple(seen) == ("normal", "halted", "reopening", "cooldown", "normal")
    assert set(seen) | {"limit_state"} == set(PHASES)
    assert machine.update(halted=False, limit_state=True).phase == "limit_state"


def test_a_backwards_clock_does_not_grant_entries(now):
    machine, box = _machine(now)
    machine.update(halted=True)
    machine.update(halted=False)
    machine.note_reopen()
    box[0] = now - timedelta(seconds=60)
    state = machine.update(halted=False)
    assert state.phase == "cooldown"
    assert state.allow_entries is False
    assert state.cooldown_s == 0.0


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_a_bad_cooldown_is_rejected_at_construction(now, bad):
    with pytest.raises(ValueError):
        HaltMachine(lambda: now, cooldown_s=bad)
