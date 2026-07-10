from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Player:
    sofa_id: int
    full_name: str
    short_name: str
    country_code: str
    slug: str
    ranking: Optional[int] = None


@dataclass(frozen=True)
class Match:
    sofa_id: int
    tour: str                         # "atp" | "wta"
    tournament_slug: str              # e.g. "rome"
    tournament_name: str              # e.g. "ATP Rome Masters"
    tournament_tier: str              # "GS" | "M1000" | "W1000" | "ATP500" | "WTA500" | "ATP250" | "WTA250" | "OTHER"
    surface: str                      # "Clay" | "Hard" | "Grass" | ...
    year: int
    round_name: str                   # "Round of 16" | "Quarterfinal" | ...
    round_short: str                  # "R16" | "QF" | "SF" | "F" | ...
    start_utc: datetime               # tz-aware UTC
    player_a: Player
    player_b: Player
    score_breakdown: dict = field(default_factory=dict)
    score: float = 0.0
    court: Optional[str] = None        # e.g. "Centre Court" (tennis), best-effort
    # When a busy-event partially overlaps the natural match window, the
    # busy filter clips the event to the largest free contiguous segment
    # and records the clipped window here. Calendar event creation prefers
    # these if set.
    event_clip_start_utc: Optional[datetime] = None
    event_clip_end_utc: Optional[datetime] = None
