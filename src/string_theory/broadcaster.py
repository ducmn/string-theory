"""UK broadcaster lookup. Best-effort, hand-curated for 2026.

The user's options are NowTV, BBC iPlayer, and TNT Sports on HBO Max.
"""
from __future__ import annotations


_RIGHTS_BY_SLUG = {
    # Tennis
    "wimbledon": "BBC iPlayer",
    "roland-garros": "TNT Sports on HBO Max",
    "australian-open": "TNT Sports on HBO Max",
    "us-open": "NowTV",
    # Football — UK rights as of 2026
    "uefa-champions-league": "TNT Sports / Amazon Prime",
    "uefa-europa-league": "TNT Sports",
    "uefa-europa-conference-league": "TNT Sports",
    # Sofascore's slug for the FIFA World Cup is "world-championship".
    "world-championship": "BBC iPlayer / ITVX",
    "european-championship": "BBC iPlayer / ITVX",
    "copa-america": "Premier Sports",
    "concacaf-champions-cup": "Premier Sports",
    "fa-cup": "BBC iPlayer / ITVX",
    "copa-del-rey": "Premier Sports",
    "coppa-italia": "Premier Sports",
    "dfb-pokal": "Sky Sports",
}


def uk_broadcaster(tournament_slug: str) -> str:
    """Return the UK broadcaster for a tournament slug.

    Default fallback is NowTV (Sky Sports) — historically reliable for
    ATP/WTA Masters, 500s and 250s, and a sensible "if you're not sure,
    try Sky first" pick for unmapped events.
    """
    return _RIGHTS_BY_SLUG.get(tournament_slug, "NowTV")


# The World Cup / Euros are split match-by-match across BBC and ITV, so the
# per-tournament default ("BBC iPlayer / ITVX") can't say which one. Sofascore
# carries no TV data, so this is a MANUAL map from published listings, keyed by
# the unordered pair of team names. Best-effort: extend/replace as the bracket
# resolves; unlisted fixtures fall back to "BBC iPlayer / ITVX".
_SPLIT_TOURNAMENTS = {"world-championship", "european-championship"}
_FOOTBALL_MATCH_BROADCASTER = {
    frozenset({"Spain", "Belgium"}): "BBC iPlayer",   # QF, Fri 10 Jul 2026
    frozenset({"Norway", "England"}): "ITVX",         # QF, Sat 11 Jul 2026
}


def uk_broadcaster_for_match(m) -> str:
    """UK broadcaster for a specific match. For BBC/ITV-split tournaments,
    prefer a known per-fixture assignment; otherwise fall back to the
    per-tournament default."""
    if m.tournament_slug in _SPLIT_TOURNAMENTS:
        override = _FOOTBALL_MATCH_BROADCASTER.get(
            frozenset({m.player_a.full_name, m.player_b.full_name}))
        if override:
            return override
    return uk_broadcaster(m.tournament_slug)
