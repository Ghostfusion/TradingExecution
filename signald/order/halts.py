"""LULD/halt state machine with reopen cooldown (plan §5.3, design §6.4/§14).

Invariant enforced here: while an instrument is in a limit state or a halt it
accepts **no new entries and no marketable exits** (an exit may still be *rested*
as a limit - the caller keeps that door open by reading ``allow_exits`` as
"marketable exit permitted"); and after a halt reopens, entries stay blocked for
the cooldown (300 s default, plan §5.3) until it decays to ``normal``.

The phase is a deterministic function of the inputs and the injected clock, and
``update`` is idempotent: calling it twice with the same arguments returns the
same state and advances nothing. The machine never grants permission from an
absence of information - a halt whose reopen has not been confirmed by the
exchange stays in ``reopening`` and keeps every entry blocked (design §14:
"never blind-market orders into a reopening auction").

Transitions::

    normal ──halted──▶ halted ──cleared──▶ reopening ──note_reopen──▶ cooldown
      │                  │                    │                          │
      │                  └──limit_state──▶ limit_state   (halt dominates) │
      │                                                                   ▼
      └────────────────────────── elapsed ≥ cooldown_s ────────────── normal

A halt that *begins* within the last :data:`NO_REOPEN_MINUTES` of the session
never reopens (plan §5.3): the phase stays ``halted`` with reason
``no_reopen_last_10m`` and the instrument is left to the closing procedures.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..risk.gate import REOPEN_COOLDOWN_S

#: The closed set of phases (design §14). ``phase`` is always a member.
PHASES: tuple[str, ...] = ("normal", "limit_state", "halted", "reopening", "cooldown")

#: A halt beginning within this many minutes of the close never reopens (plan §5.3).
NO_REOPEN_MINUTES = 10.0

#: Default reopen cooldown: no entries for 5 minutes after a reopen (plan §5.3).
#: The number is owned by ``signald.risk.gate`` so the machine and the gate can
#: never disagree about how long a reopen stays off limits.
DEFAULT_COOLDOWN_S = REOPEN_COOLDOWN_S


@dataclass(frozen=True)
class HaltState:
    """One instrument's halt/limit posture at a decision point.

    ``cooldown_s`` is the number of seconds the reopen cooldown has been running
    (0 outside it), matching ``MarketState.reopen_cooldown_s`` as the gate reads
    it: while the value is below the configured cooldown the reopen is still
    inside the no-entry window. ``allow_entries`` is the authoritative verdict -
    it is True only in ``normal``. ``allow_exits`` is False while
    halted/limit/reopening: an exit there must be *rested* as a limit (never a
    marketable order into a halt or a reopening auction).
    """

    phase: str
    allow_entries: bool
    allow_exits: bool
    cooldown_s: float
    reason: str
    since: str | None = None


class HaltMachine:
    """Deterministic per-instrument halt state machine driven by an injected clock."""

    def __init__(
        self, now: Callable[[], datetime], *, cooldown_s: float = DEFAULT_COOLDOWN_S
    ) -> None:
        if not (math.isfinite(cooldown_s) and cooldown_s > 0.0):
            raise ValueError(f"cooldown_s must be a positive number of seconds, got {cooldown_s!r}")
        self._now = now
        self._cooldown_s = float(cooldown_s)
        self._phase = "normal"
        self._reason = "normal"
        self._since: datetime | None = None
        #: When the exchange reopen was noted; only meaningful during ``cooldown``.
        self._reopen_at: datetime | None = None
        #: Set once a halt begins inside the last 10 minutes: it can never reopen.
        self._no_reopen = False

    def update(
        self,
        *,
        halted: bool,
        limit_state: bool = False,
        now: datetime | None = None,
        minutes_to_close: float | None = None,
    ) -> HaltState:
        """Advance from the venue's current state and return the new posture.

        ``halted`` is a full trading halt; ``limit_state`` is an LULD limit state
        (a halt is a superset, so ``halted`` wins). ``minutes_to_close`` is the
        time to the session close when known - a halt inside the final 10
        minutes is flagged as never-reopening.
        """
        at = now if now is not None else self._now()

        if halted:
            if self._phase != "halted":
                self._phase, self._since = "halted", at
            self._reopen_at = None
            self._reason = "halt"
            if minutes_to_close is not None and minutes_to_close <= NO_REOPEN_MINUTES:
                self._no_reopen = True
                self._reason = "no_reopen_last_10m"
            return self._snapshot(at)

        if self._no_reopen:
            # The halt began inside the last 10 minutes: it never reopens, and
            # neither a clearing nor a limit state revives it (plan §5.3).
            if self._phase != "halted":
                self._phase, self._since = "halted", at
            self._reopen_at = None
            self._reason = "no_reopen_last_10m"
            return self._snapshot(at)

        if limit_state:
            if self._phase != "limit_state":
                self._phase, self._since = "limit_state", at
            self._reopen_at = None
            self._reason = "limit_state"
            return self._snapshot(at)

        if self._phase in ("halted", "limit_state"):
            # The halt has cleared, but the reopen is not confirmed yet: block
            # entries and stay off the reopening auction until `note_reopen`.
            self._phase, self._since = "reopening", at
            self._reason = "reopening"
            self._reopen_at = None

        if self._phase == "cooldown":
            elapsed = self._elapsed(at)
            if elapsed >= self._cooldown_s:
                self._phase, self._since = "normal", at
                self._reason = "normal"
                self._reopen_at = None
        elif self._phase == "normal":
            self._reason = "normal"
            if self._since is None:
                self._since = at

        return self._snapshot(at)

    def note_reopen(self, now: datetime | None = None) -> None:
        """Record the exchange's reopen and start the cooldown (plan §5.3).

        Only meaningful after a clear has been observed (phase ``reopening``).
        It is ignored while halted/limit and for the never-reopen episode, so a
        caller cannot reopen an instrument the session has already closed out.
        """
        if self._no_reopen or self._phase != "reopening":
            return
        at = now if now is not None else self._now()
        self._phase = "cooldown"
        self._since = at
        self._reopen_at = at
        self._reason = "cooldown"

    def state(self) -> HaltState:
        """The current posture, stamped from the injected clock."""
        return self._snapshot(self._now())

    # -- internals -------------------------------------------------------
    def _elapsed(self, at: datetime) -> float:
        if self._reopen_at is None:
            return 0.0
        # A backwards clock must not extend the cooldown (fail closed on the
        # remaining time, never grant entries): clamp at zero.
        return max(0.0, (at - self._reopen_at).total_seconds())

    def _snapshot(self, at: datetime) -> HaltState:
        phase = self._phase
        elapsed = self._elapsed(at) if phase == "cooldown" else 0.0
        return HaltState(
            phase=phase,
            allow_entries=phase == "normal",
            allow_exits=phase in ("normal", "cooldown"),
            cooldown_s=elapsed,
            reason=self._reason,
            since=self._since.isoformat() if self._since is not None else None,
        )
