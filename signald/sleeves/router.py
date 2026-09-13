"""Sleeve routing: a decision/signal is assigned to exactly one sleeve (design §3.5).

The router is the **only** producer of a sleeve name (design §3.5 R4, plan §2.3).
A research artifact always lands in the swing sleeve; the producer may not
choose the sleeve, so an artifact that carries a ``sleeve`` field is rejected
unless it agrees with the router. This is fail closed: an artifact that tries to
route itself is quarantined upstream rather than silently honoured.

The intraday engine has two independent opt-ins - the config key
``intraday_enabled`` and the caller's ``enabled`` flag - so the intraday sleeve
cannot start by accident (plan §§3, 5).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..risk.state import INTRADAY, SWING
from ..schema import ResearchDecision


class RouteError(ValueError):
    """A decision cannot be routed to a sleeve (fail closed; quarantine upstream)."""


@dataclass(frozen=True)
class Route:
    """The sleeve a decision belongs to, and which producer emitted it."""

    sleeve: str  # "swing" | "intraday"
    source: str  # "research" | "engine"
    reason: str


def route_research(rd: ResearchDecision, cfg: Config) -> Route:
    """Route a research decision to the swing sleeve (design §3.5 R1).

    Raises :class:`RouteError` when the artifact tries to choose a sleeve other
    than ``swing`` - research informs, it never selects the sleeve.
    """
    declared = rd.extra.get("sleeve")
    if declared is not None and str(declared).strip().lower() != SWING:
        raise RouteError(
            f"research artifact {rd.ticker!r} declares sleeve={declared!r}; "
            f"the router owns sleeve selection and research always routes to {SWING!r}"
        )
    return Route(
        sleeve=SWING,
        source="research",
        reason="research artifacts are advisory and always land in the swing sleeve",
    )


def route_intraday(cfg: Config, *, enabled: bool) -> Route:
    """Route the intraday engine's own signal to the intraday sleeve.

    Requires both opt-ins (config ``intraday_enabled`` and the caller's
    ``enabled`` flag); otherwise raises :class:`RouteError`.
    """
    if not (cfg.intraday_enabled and enabled):
        raise RouteError(
            "intraday sleeve is not enabled: "
            f"config intraday_enabled={cfg.intraday_enabled!r}, caller enabled={enabled!r}; "
            "both opt-ins are required"
        )
    return Route(
        sleeve=INTRADAY,
        source="engine",
        reason="intraday engine signal with both opt-ins set",
    )
