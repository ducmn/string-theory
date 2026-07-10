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
