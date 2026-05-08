from dataclasses import replace
from datetime import datetime, timedelta, timezone

from string_theory.conflicts import filter_against_busy, pick_non_overlapping
from string_theory.models import Match, Player


def make_match(*, sofa_id: int, start: datetime, round_short: str = "R32", score: float = 6.0) -> Match:
    p = Player(sofa_id=sofa_id * 10, full_name=f"P{sofa_id}", short_name=f"P{sofa_id}",
               country_code="USA", slug=f"p{sofa_id}", ranking=10)
    q = Player(sofa_id=sofa_id * 10 + 1, full_name=f"Q{sofa_id}", short_name=f"Q{sofa_id}",
               country_code="USA", slug=f"q{sofa_id}", ranking=20)
    return Match(
        sofa_id=sofa_id, tour="atp", tournament_slug="rome", tournament_name="ATP Rome Masters",
        tournament_tier="M1000", surface="clay", year=2026,
        round_name="Round of 32", round_short=round_short, start_utc=start,
        player_a=p, player_b=q, score=score,
    )


def test_dedup_keeps_higher_score_when_overlap():
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    a = make_match(sofa_id=1, start=t, round_short="R32", score=7.0)            # 14:00, 2h block
    b = make_match(sofa_id=2, start=t + timedelta(minutes=30), score=11.0)      # overlaps, higher score
    c = make_match(sofa_id=3, start=t + timedelta(hours=3), score=6.5)          # no overlap with either

    kept = pick_non_overlapping([a, b, c])
    assert {m.sofa_id for m in kept} == {2, 3}
    # Returned in chronological order.
    assert [m.sofa_id for m in kept] == [2, 3]


def test_dedup_no_overlap_keeps_all():
    t = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc)
    matches = [
        make_match(sofa_id=1, start=t),
        make_match(sofa_id=2, start=t + timedelta(hours=3)),
        make_match(sofa_id=3, start=t + timedelta(hours=6)),
    ]
    kept = pick_non_overlapping(matches)
    assert len(kept) == 3


def test_busy_filter_drops_overlapping():
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    matches = [
        make_match(sofa_id=1, start=t),                                  # busy here
        make_match(sofa_id=2, start=t + timedelta(hours=4)),             # free
    ]
    busy = [(t - timedelta(minutes=30), t + timedelta(minutes=30))]      # blocks #1
    out = filter_against_busy(matches, busy)
    assert [m.sofa_id for m in out] == [2]


def test_busy_filter_empty_passthrough():
    t = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc)
    matches = [make_match(sofa_id=1, start=t)]
    assert filter_against_busy(matches, []) == matches


def test_favorite_wins_tiebreaker_on_overlap():
    """When two overlapping matches tie on score, the one featuring a named
    favorite (favorite_bonus > 0) keeps its slot."""
    t = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    a = replace(make_match(sofa_id=1, start=t, score=9.0),
                score_breakdown={"favorite": 0.0, "total": 9.0})
    b = replace(make_match(sofa_id=2, start=t + timedelta(minutes=10), score=9.0),
                score_breakdown={"favorite": 2.0, "total": 9.0})
    kept = pick_non_overlapping([a, b])
    assert [m.sofa_id for m in kept] == [2]
