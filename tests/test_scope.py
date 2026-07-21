"""Which tournaments are in scope at all.

The user only watches Wimbledon (tennis) and the World Cup (football);
everything else is filtered out before scoring even happens.
"""
from string_theory.scrape import (
    FOOTBALL_ALLOWLIST,
    FOOTBALL_NATIONAL_TEAM_SLUGS,
    TENNIS_ALLOWLIST,
    normalize_events,
)


def _tennis_event(slug: str, sofa_id: int = 1) -> dict:
    """Minimal Sofascore-shaped singles event for the given tournament slug."""
    return {
        "id": sofa_id,
        "status": {"type": "notstarted"},
        "startTimestamp": 1_784_000_000,
        "eventFilters": {"category": ["singles"]},
        "homeTeam": {"id": 10, "name": "A Player", "shortName": "A. Player"},
        "awayTeam": {"id": 11, "name": "B Player", "shortName": "B. Player"},
        "roundInfo": {"name": "Semifinal"},
        "season": {"year": 2026},
        "tournament": {
            "uniqueTournament": {
                "slug": slug,
                "name": slug.replace("-", " ").title(),
                "tennisPoints": 2000,
                "category": {"slug": "atp"},
            }
        },
    }


def test_only_wimbledon_survives_normalisation():
    """A Wimbledon match is kept; other slams and tour events are dropped."""
    events = [
        _tennis_event("wimbledon", 1),
        _tennis_event("us-open", 2),
        _tennis_event("roland-garros", 3),
        _tennis_event("australian-open", 4),
        _tennis_event("bastad", 5),
    ]
    kept = normalize_events(events, rankings={})
    assert [m.tournament_slug for m in kept] == ["wimbledon"]


def test_tennis_allowlist_is_wimbledon_only():
    assert TENNIS_ALLOWLIST == {"wimbledon"}


def test_football_allowlist_is_world_cup_only():
    """Only the World Cup — no club competitions, Euros, or Copa."""
    assert set(FOOTBALL_ALLOWLIST) == {"world-championship"}
    # The World Cup is still treated as a national-team comp, so the
    # England/France nation filter (and its final exemption) still applies.
    assert "world-championship" in FOOTBALL_NATIONAL_TEAM_SLUGS


def test_world_cup_knockout_rounds_qualify():
    rounds = FOOTBALL_ALLOWLIST["world-championship"]
    for r in ("Round of 16", "Quarterfinal", "Semifinal", "Final"):
        assert r in rounds
