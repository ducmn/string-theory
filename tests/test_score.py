from datetime import datetime, timezone

import pytest

from string_theory.models import Match, Player
from string_theory.score import (
    PUSH_THRESHOLD,
    favorite_bonus,
    headliner_bonus,
    is_pushable,
    ranking_score,
    score_match,
)


def make_match(
    *,
    tier="M1000",
    round_short="R16",
    rank_a=10,
    rank_b=10,
    name_a="Anon A",
    name_b="Anon B",
):
    return Match(
        sofa_id=1,
        tour="atp",
        tournament_slug="rome",
        tournament_name="ATP Rome Masters",
        tournament_tier=tier,
        surface="clay",
        year=2026,
        round_name="Round of 16",
        round_short=round_short,
        start_utc=datetime(2026, 5, 8, 17, 0, tzinfo=timezone.utc),
        player_a=Player(
            sofa_id=1, full_name=name_a, short_name=name_a, country_code="USA",
            slug=name_a.lower().replace(" ", "-"), ranking=rank_a,
        ),
        player_b=Player(
            sofa_id=2, full_name=name_b, short_name=name_b, country_code="USA",
            slug=name_b.lower().replace(" ", "-"), ranking=rank_b,
        ),
    )


# ---- ranking_score ----------------------------------------------------------

@pytest.mark.parametrize(
    "a,b,expected",
    [
        (1, 2, 5.0),       # both top 10
        (10, 10, 5.0),
        (5, 15, 3.0),      # top10 vs top20
        (10, 20, 3.0),
        (40, 30, 2.0),     # both top 50
        (51, 50, 1.0),     # both top 100
        (100, 100, 1.0),
        (101, 1, 0.0),     # one outside top 100
        (None, 5, 0.0),    # one unranked
        (None, None, 0.0),
        (200, 300, 0.0),
    ],
)
def test_ranking_score(a, b, expected):
    assert ranking_score(a, b) == expected
    assert ranking_score(b, a) == expected  # symmetric


# ---- favorite_bonus --------------------------------------------------------

def test_favorite_bonus_when_either_player_favorite():
    assert favorite_bonus("Jannik Sinner", "Random Guy") == 2.0
    assert favorite_bonus("Random Guy", "Carlos Alcaraz") == 2.0
    assert favorite_bonus("Random A", "Random B") == 0.0


def test_learner_tien_is_a_favorite():
    """Vietnamese-American ATP rising star — explicit personal pick."""
    assert favorite_bonus("Learner Tien", "Random Guy") == 2.0


# ---- score_match end-to-end ------------------------------------------------

# ---- headliner_bonus -------------------------------------------------------

def test_headliner_bonus_for_top_5():
    assert headliner_bonus(1, 99) == 2.0
    assert headliner_bonus(99, 5) == 2.0
    assert headliner_bonus(6, 7) == 0.0
    assert headliner_bonus(None, None) == 0.0


# ---- score_match end-to-end ------------------------------------------------

def test_grand_slam_final_top_players_scores_high():
    m = make_match(
        tier="GS", round_short="F",
        rank_a=1, rank_b=2,
        name_a="Jannik Sinner", name_b="Carlos Alcaraz",
    )
    scored = score_match(m)
    # tier 5 + round 5 + ranking 5 + favorite 2 + headliner 2 = 19
    assert scored.score == 19.0
    assert scored.score_breakdown == {
        "tier": 5.0, "round": 5.0, "ranking": 5.0, "favorite": 2.0, "headliner": 2.0,
        "total": 19.0,
    }


def test_atp_250_first_round_unranked_scores_low():
    m = make_match(tier="ATP250", round_short="R32", rank_a=180, rank_b=220)
    scored = score_match(m)
    # tier 1 + round 1 + ranking 0 + favorite 0 + headliner 0 = 2
    assert scored.score == 2.0


def test_unknown_tier_or_round_does_not_crash():
    m = make_match(tier="OTHER", round_short="R128", rank_a=5, rank_b=5)
    scored = score_match(m)
    # tier 0 + round 0 + ranking 5 + favorite 0 + headliner 2 = 7
    assert scored.score == 7.0


def test_is_pushable_uses_threshold():
    m_high = score_match(make_match(tier="M1000", round_short="QF", rank_a=5, rank_b=15))
    # 4 + 3 + 3 + 0 + 2 = 12
    assert m_high.score == 12.0
    assert is_pushable(m_high)

    m_low = score_match(make_match(tier="ATP250", round_short="R32", rank_a=80, rank_b=90))
    # 1 + 1 + 1 + 0 + 0 = 3
    assert m_low.score == 3.0
    assert not is_pushable(m_low)


def test_top5_player_in_m1000_first_round_is_pushable():
    """Acceptance test: Djokovic (#4) vs Prižmić (#79) in Rome R64 must push."""
    m = make_match(
        tier="M1000", round_short="R64", rank_a=4, rank_b=79,
        name_a="Novak Djokovic", name_b="Dino Prižmić",
    )
    scored = score_match(m)
    # 4 + 0.5 + 1 + 0 + 2 = 7.5
    assert scored.score == 7.5
    assert is_pushable(scored)


def test_default_threshold_is_six():
    assert PUSH_THRESHOLD == 6.0
