"""
The TradeIdea contract + Engine base class — the one schema every strategy speaks.

Phase 0 cornerstone (ARCHITECTURE.md §5/§6). Introduced ADDITIVELY (strangler
pattern): new engines emit `TradeIdea`s; the existing signal paths are migrated
onto this contract one at a time, and the old duplicated paths are deleted only
once the new path serves green.

Audit rules baked in:
  * DEFINED-RISK IS MANDATORY. Every actionable idea must declare a finite,
    positive `max_loss_rupees`; `validate()` rejects uncapped risk so the Risk
    Engine can never size an open-ended loss. Naked short options are banned.
  * The schema carries `expiry`, `legs`, `greeks`, `margin_required`, and an
    `adjustment_plan` so the Risk Engine + Scorecard have everything they need
    for F&O income (index, European, cash-settled by policy).
  * `scorecard_id` ties an emitted idea to its realized outcome so the Scorecard
    can match predicted -> actual with no post-hoc selection.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from math import isfinite
from typing import Optional


class Structure(str, Enum):
    """What KIND of position the idea is. Drives risk treatment + UI."""
    CASH_LONG       = "CASH_LONG"
    CASH_SHORT      = "CASH_SHORT"
    FUTURE_LONG     = "FUTURE_LONG"
    FUTURE_SHORT    = "FUTURE_SHORT"
    OPT_LONG        = "OPT_LONG"          # debit single — risk = premium paid
    OPT_SHORT       = "OPT_SHORT"         # naked credit — UNBOUNDED, banned hands-off
    VERTICAL_SPREAD = "VERTICAL_SPREAD"   # defined-risk credit/debit spread
    IRON_CONDOR     = "IRON_CONDOR"       # defined-risk income workhorse (neutral)
    STRANGLE        = "STRANGLE"          # short strangle — must be wing-capped
    STRADDLE        = "STRADDLE"
    CALENDAR        = "CALENDAR"
    PAIR            = "PAIR"          # market-neutral cash pair (long A / short β·B)
    CUSTOM          = "CUSTOM"

    @property
    def is_option(self) -> bool:
        return self in {
            Structure.OPT_LONG, Structure.OPT_SHORT, Structure.VERTICAL_SPREAD,
            Structure.IRON_CONDOR, Structure.STRANGLE, Structure.STRADDLE,
            Structure.CALENDAR, Structure.CUSTOM,
        }

    @property
    def is_naked_short(self) -> bool:
        """Structures that carry open-ended loss unless explicitly capped."""
        return self in {
            Structure.OPT_SHORT, Structure.STRANGLE, Structure.STRADDLE,
            Structure.CASH_SHORT, Structure.FUTURE_SHORT,
        }


class Horizon(str, Enum):
    INTRADAY = "intraday"
    SWING    = "swing"      # ~5 sessions
    ROTATION = "rotation"   # ~1wk-1mo
    LONG     = "long"       # months
    EXPIRY   = "expiry"     # to the option expiry (F&O income)


@dataclass
class Leg:
    """One instrument leg of a (possibly multi-leg) idea."""
    symbol: str                      # tradingsymbol, e.g. 'NIFTY24JUL24500CE' or 'RELIANCE'
    side: int                        # +1 buy / -1 sell
    qty: int                         # units; lot math handled by the sizer
    strike: Optional[float] = None
    expiry: Optional[date] = None
    opt_type: Optional[str] = None   # 'CE' | 'PE' | None for cash/future


@dataclass
class Greeks:
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None


@dataclass
class TradeIdea:
    """The one schema every engine emits. Cash and F&O both fit here."""
    engine: str                          # which engine produced it
    symbol: str                          # underlying / instrument the idea is ABOUT
    structure: Structure
    horizon: Horizon
    direction: int                       # +1 bullish / -1 bearish / 0 neutral (e.g. condor)

    # Cash levels (CASH_*/FUTURE_*); None for pure option structures.
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    qty: Optional[int] = None                # sized quantity (cash/future); options use legs
    capital_required: Optional[float] = None

    # Option-structure economics.
    legs: list[Leg] = field(default_factory=list)
    net_credit: Optional[float] = None       # +credit received / -debit paid (per lot)
    max_profit: Optional[float] = None
    breakevens: list[float] = field(default_factory=list)
    greeks: Optional[Greeks] = None
    margin_required: Optional[float] = None
    expiry: Optional[date] = None

    # RISK — defined-risk is mandatory for any actionable idea.
    max_loss_rupees: Optional[float] = None

    # Conviction (calibrated, net-of-cost) + provenance.
    confidence: Optional[float] = None       # 0..1
    expected_value: Optional[float] = None   # net-of-cost EV (rupees or fraction)
    thesis_ref: Optional[str] = None         # link to a Research Brain note
    reasons: list[str] = field(default_factory=list)
    adjustment_plan: Optional[str] = None    # how the engine intends to manage/exit

    # Lifecycle / identity.
    idea_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    scorecard_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    valid_until: Optional[str] = None        # ISO; after this the idea is stale

    @property
    def is_actionable(self) -> bool:
        # Neutral structures (condor, market-neutral pair) are actionable despite direction 0.
        return self.direction != 0 or self.structure in {
            Structure.IRON_CONDOR, Structure.CALENDAR, Structure.PAIR, Structure.CUSTOM,
        }

    def validate(self) -> "TradeIdea":
        """Enforce the non-negotiables. Raises ValueError on an unsafe idea.

        The Risk Engine calls this before sizing; engines call it before emitting.
        The single most important rule: NO UNCAPPED RISK.
        """
        # 1. Defined-risk is mandatory: a finite, positive capped loss.
        if (self.max_loss_rupees is None
                or not isfinite(self.max_loss_rupees)
                or self.max_loss_rupees <= 0):
            raise ValueError(
                f"{self.engine}/{self.symbol}: max_loss_rupees must be a finite positive "
                f"number (defined-risk is mandatory); got {self.max_loss_rupees!r}"
            )
        # 2. Naked short options are banned outright (hands-off safety).
        if self.structure is Structure.OPT_SHORT:
            raise ValueError(
                f"{self.engine}/{self.symbol}: naked OPT_SHORT is banned — wrap it in a "
                f"defined-risk spread/condor."
            )
        # 3. Cash/future ideas need entry+stop (that's what bounds the loss).
        if self.structure in {
            Structure.CASH_LONG, Structure.CASH_SHORT,
            Structure.FUTURE_LONG, Structure.FUTURE_SHORT,
        }:
            if self.entry is None or self.stop is None:
                raise ValueError(f"{self.engine}/{self.symbol}: cash/future idea needs entry+stop")
        # 4. Option structures need legs.
        if self.structure.is_option and not self.legs:
            raise ValueError(f"{self.engine}/{self.symbol}: option structure needs legs")
        # 4b. A market-neutral pair must carry both of its legs (long A + short β·B);
        #     its defined risk comes from the spread z-stop, captured in max_loss_rupees.
        if self.structure is Structure.PAIR and len(self.legs) < 2:
            raise ValueError(f"{self.engine}/{self.symbol}: PAIR needs both legs (long + short)")
        # 5. Confidence sanity.
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"{self.engine}/{self.symbol}: confidence must be in [0,1]")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


class Engine(ABC):
    """Base class for every strategy engine. Emits TradeIdeas; never places orders.

    Engines are PURE PRODUCERS: read data, emit ideas. The Risk Engine gates them,
    the Alerter surfaces them, the human confirms, execution places. This
    separation is what lets us add/remove engines without touching the money path.
    """

    #: short stable id, e.g. "fno_income"
    name: str = "engine"
    #: default horizon for this engine's ideas
    horizon: Horizon = Horizon.SWING
    #: how often scan() should run, in seconds (None = event/EOD driven)
    clock_seconds: Optional[int] = None

    @abstractmethod
    def scan(self) -> list[TradeIdea]:
        """Produce the current candidate TradeIdeas. Read-only; no side effects."""
        ...

    def healthcheck(self) -> dict:
        """Optional self-report (data freshness, last scan, etc.)."""
        return {"engine": self.name, "ok": True}


__all__ = ["Structure", "Horizon", "Leg", "Greeks", "TradeIdea", "Engine"]
