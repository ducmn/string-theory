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

def test_favorite_bonus_named_players_only():
    """A named favorite gets the bonus; unlisted players don't."""
    assert favorite_bonus("Learner Tien", "Random Guy") == 2.0
    assert favorite_bonus("Random Guy", "Learner Tien") == 2.0
    assert favorite_bonus("Jannik Sinner", "Random Guy") == 2.0  # Sinner is a favorite
    assert favorite_bonus("Carlos Alcaraz", "Daniil Medvedev") == 0.0  # neither is
    assert favorite_bonus("Random A", "Random B") == 0.0


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
        name_a="Carlos Alcaraz", name_b="Daniil Medvedev",  # top players, not favorites
    )
    scored = score_match(m)
    # tier 5 + round 5 + ranking 5 + favorite 0 (neither is a favorite) +
    # headliner 2 (both top-5) = 17
    assert scored.score == 17.0
    assert scored.score_breakdown == {
        "tier": 5.0, "round": 5.0, "ranking": 5.0, "favorite": 0.0, "headliner": 2.0,
        "total": 17.0,
    }


def test_learner_tien_match_gets_favorite_bonus():
    m = make_match(
        tier="M1000", round_short="R32",
        rank_a=21, rank_b=11,
        name_a="Learner Tien", name_b="Alexander Bublik",
    )
    scored = score_match(m)
    # tier 4 + round 1 + ranking 3 (better<=10, worse<=20... 11 & 21 -> both<=50 =2)
    # ranking(11,21): better=11,worse=21 -> not both<=20 -> both<=50 => 2
    # + favorite 2 (Tien) + headliner 0 = 9
    assert scored.score_breakdown["favorite"] == 2.0
    assert scored.score == 9.0


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


def test_djokovic_prizmic_m1000_r64_is_pushable_at_threshold_seven():
    """At PUSH_THRESHOLD=7, a top-5 vs outside-top-50 R64 with headliner
    bonus scores 7.5 and just clears."""
    m = make_match(
        tier="M1000", round_short="R64", rank_a=4, rank_b=79,
        name_a="Novak Djokovic", name_b="Dino Prižmić",
    )
    scored = score_match(m)
    assert scored.score == 7.5
    assert is_pushable(scored)


def test_low_scored_tien_match_is_still_pushable_via_favorite_shortcut():
    """Even a low-scored match featuring Tien clears the threshold."""
    m = make_match(
        tier="ATP250", round_short="R32", rank_a=21, rank_b=120,
        name_a="Learner Tien", name_b="Some Qualifier",
    )
    scored = score_match(m)
    # tier 1 + round 1 + ranking 0 + favorite 2 + headliner 0 = 4 — below 7
    assert scored.score < PUSH_THRESHOLD
    # But favorite shortcut keeps it pushable
    assert is_pushable(scored)


def test_default_threshold_is_seven():
    assert PUSH_THRESHOLD == 7.0


def test_iga_r128_score_seven_just_clears_threshold():
    """Swiatek (#3) vs unranked qualifier in RG R128 scores 7.0 (headliner
    bonus only — she's not a favorite). At threshold 7 it just clears."""
    m = make_match(
        tier="GS", round_short="R128", rank_a=3, rank_b=136,
        name_a="Iga Swiatek", name_b="E. Jones",
    )
    scored = score_match(m)
    assert scored.score == 7.0
    assert is_pushable(scored)


def test_favorites_are_user_named_picks():
    """The user's named picks: Tien (headline), Dimitrov, Keys, Eala, plus
    Raducanu and Fery on the British side. The wider British contingent was
    dropped 2026-07-10 — the user said they only care about Emma and Fery."""
    from string_theory.score import favorite_bonus, FAVORITES
    for name in ("Learner Tien", "Grigor Dimitrov", "Madison Keys",
                 "Alexandra Eala", "Jannik Sinner", "Emma Raducanu", "Arthur Fery"):
        assert name in FAVORITES, f"{name} should be a favorite"
    # Dropped Brits are no longer favorites
    for dropped in ("Katie Boulter", "Jack Draper", "Cameron Norrie",
                    "Dan Evans", "Sonay Kartal"):
        assert dropped not in FAVORITES, f"{dropped} should have been dropped"
    # Non-favorites stay non-favorites
    assert favorite_bonus("Iga Swiatek", "Random") == 0.0
    assert favorite_bonus("Alex de Minaur", "Random") == 0.0
    assert favorite_bonus("Madison Keys", "Random") == 2.0
    assert favorite_bonus("Learner Tien", "Random") == 2.0


def test_football_favorite_bonus_for_england():
    """A World Cup match featuring England gets the favorite bonus so it wins
    dedup against a same-slot non-favorite football match."""
    def fb(home, away):
        return Match(
            sofa_id=1, tour="football", tournament_slug="world-championship",
            tournament_name="FIFA World Cup", tournament_tier="FOOTBALL",
            surface="", year=2026, round_name="Quarterfinals", round_short="Quarterfinals",
            start_utc=datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc),
            player_a=Player(sofa_id=1, full_name=home, short_name=home,
                            country_code="", slug=home.lower(), ranking=None),
            player_b=Player(sofa_id=2, full_name=away, short_name=away,
                            country_code="", slug=away.lower(), ranking=None),
        )

    eng = score_match(fb("Norway", "England"))
    other = score_match(fb("Spain", "Belgium"))
    assert eng.score_breakdown["favorite"] == 2.0
    assert other.score_breakdown["favorite"] == 0.0
    assert eng.score > other.score


def test_non_gs_at_score_8_is_pushable_again():
    """At threshold 7 the boring Auger-Kovacevic 8 sneaks back in — the
    user accepts that as the cost of catching Iga's 7.0 RG R128."""
    m = make_match(
        tier="ATP500", round_short="R16", rank_a=5, rank_b=110,
        name_a="Felix Auger-Aliassime", name_b="Aleksandar Kovacevic",
    )
    scored = score_match(m)
    # tier 3 + round 2 + ranking 0 (outside top-100) + favorite 0 +
    # headliner 2 (Auger top-5) = 7.0 — just clears threshold of 7
    assert scored.score == 7.0
    assert is_pushable(scored)
