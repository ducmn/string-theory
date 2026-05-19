"""Match scoring.

Hardcoded weights, tunable. v2 may switch to LLM-based; the function signature
should stay stable so the rest of the pipeline doesn't care.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .models import Match


# --- Tunable constants -------------------------------------------------------

PUSH_THRESHOLD: float = 6.0

TIER_WEIGHT = {
    "GS": 5.0,
    "M1000": 4.0,
    "W1000": 4.0,
    "ATP500": 3.0,
    "WTA500": 3.0,
    "ATP250": 1.0,
    "WTA250": 1.0,
}

ROUND_WEIGHT = {
    "F": 5.0,
    "SF": 4.0,
    "QF": 3.0,
    "R16": 2.0,
    "R32": 1.0,
    "R64": 0.5,
    "R128": 0.0,
}

FAVORITES = {
    # Personal pick — Vietnamese-American rising ATP player. The only
    # name the user wants the favorite bonus to apply to; the headliner
    # bonus still independently rewards any current top-5 player.
    "Learner Tien",
}

UNRANKED = 9999


def ranking_score(rank_a: Optional[int], rank_b: Optional[int]) -> float:
    a = rank_a if rank_a else UNRANKED
    b = rank_b if rank_b else UNRANKED
    better, worse = (a, b) if a <= b else (b, a)
    if better <= 10 and worse <= 10:
        return 5.0
    if better <= 10 and worse <= 20:
        return 3.0
    if better <= 50 and worse <= 50:
        return 2.0
    if better <= 100 and worse <= 100:
        return 1.0
    return 0.0


def favorite_bonus(name_a: str, name_b: str) -> float:
    return 2.0 if (name_a in FAVORITES or name_b in FAVORITES) else 0.0


def headliner_bonus(rank_a: Optional[int], rank_b: Optional[int]) -> float:
    """Top-5 players are must-watch even against weak opposition.

    Without this, Djokovic vs Prižmić in an M1000 R64 scores 5.5 and gets
    silently dropped under the 6.0 threshold — that's the wrong call.
    """
    best = min(rank_a or UNRANKED, rank_b or UNRANKED)
    return 2.0 if best <= 5 else 0.0


def score_match(m: Match) -> Match:
    tier = TIER_WEIGHT.get(m.tournament_tier, 0.0)
    rnd = ROUND_WEIGHT.get(m.round_short, 0.0)
    rank = ranking_score(m.player_a.ranking, m.player_b.ranking)
    fav = favorite_bonus(m.player_a.full_name, m.player_b.full_name)
    headliner = headliner_bonus(m.player_a.ranking, m.player_b.ranking)
    total = tier + rnd + rank + fav + headliner
    breakdown = {
        "tier": tier,
        "round": rnd,
        "ranking": rank,
        "favorite": fav,
        "headliner": headliner,
        "total": total,
    }
    return replace(m, score=total, score_breakdown=breakdown)


def is_pushable(m: Match, threshold: float = PUSH_THRESHOLD) -> bool:
    return m.score >= threshold
