"""Match scoring.

Hardcoded weights, tunable. v2 may switch to LLM-based; the function signature
should stay stable so the rest of the pipeline doesn't care.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .models import Match


# --- Tunable constants -------------------------------------------------------

PUSH_THRESHOLD: float = 7.0

# Favorites get a lower bar than the general threshold (not a blanket free
# pass) — a favorite in a meaningful match still shows, but a minor one (a
# low-ranked favorite in an ATP250 early round, e.g. Dimitrov R16 at Båstad,
# score 5.0) no longer forces its way onto the calendar.
FAVORITE_PUSH_THRESHOLD: float = 6.0

# Tour-250 events are the lowest rung of the main tour; the user doesn't want
# them at all, not even the final (a 250 SF/F otherwise scores 7–8 on round
# weight alone). Excluded regardless of round or favorite. Bump the tier here
# (e.g. drop "ATP500"/"WTA500" in too) to be stricter.
EXCLUDED_TIERS = {"ATP250", "WTA250"}

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
    # Vietnamese-American rising ATP player — user's headline pick.
    "Learner Tien",
    # Personal picks the user named directly.
    "Grigor Dimitrov",
    "Madison Keys",
    "Alexandra Eala",
    "Jannik Sinner",
    # The Brits the user actually cares about.
    "Emma Raducanu",
    "Arthur Fery",
}

# National football teams the user follows. A match featuring one of these
# always survives dedup (see pick_non_overlapping) — matched against
# Player.full_name, which for football holds the team name.
FOOTBALL_FAVORITES = {
    "England",
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


FOOTBALL_ROUND_WEIGHT = {
    "Final": 8.0,
    "Semifinal": 6.0,
    "Quarterfinal": 4.0,
    "Round of 16": 2.0,
}


def score_match(m: Match) -> Match:
    # Football matches are pre-filtered to knockout rounds of big
    # competitions; they're always interesting and always pushable. Score
    # them high enough to clear any tennis threshold and sit comfortably
    # in dedup against tennis matches.
    if m.tour == "football":
        tier = 10.0
        rnd = FOOTBALL_ROUND_WEIGHT.get(m.round_name, 2.0)
        fav = 2.0 if (m.player_a.full_name in FOOTBALL_FAVORITES
                      or m.player_b.full_name in FOOTBALL_FAVORITES) else 0.0
        total = tier + rnd + fav
        breakdown = {
            "tier": tier, "round": rnd, "ranking": 0.0,
            "favorite": fav, "headliner": 0.0, "total": total,
        }
        return replace(m, score=total, score_breakdown=breakdown)

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


def is_pushable(m: Match, threshold: float = PUSH_THRESHOLD,
                favorite_threshold: float = FAVORITE_PUSH_THRESHOLD) -> bool:
    """Push if the match clears its threshold. A favorite gets the lower
    `favorite_threshold` (its +2 bonus plus a reduced bar) rather than a
    blanket bypass, so an unimportant favorite match — small tournament,
    early round, low-ranked — is dropped like any other lightweight match."""
    if m.tournament_tier in EXCLUDED_TIERS:
        return False
    if (m.score_breakdown or {}).get("favorite", 0.0) > 0:
        return m.score >= favorite_threshold
    return m.score >= threshold
