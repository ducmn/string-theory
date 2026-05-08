"""UK broadcaster lookup. Best-effort, hand-curated for 2026.

The user's options are NowTV, BBC iPlayer, and TNT Sports on HBO Max.
"""
from __future__ import annotations


_RIGHTS_BY_SLUG = {
    "wimbledon": "BBC iPlayer",
    "roland-garros": "TNT Sports on HBO Max",
    "australian-open": "TNT Sports on HBO Max",
    # Sky has held US Open UK rights consistently.
    "us-open": "NowTV",
}


def uk_broadcaster(tournament_slug: str) -> str:
    """Return the UK broadcaster for a tournament slug.

    Default for ATP/WTA Masters, 500s and 250s is NowTV (Sky Sports).
    """
    return _RIGHTS_BY_SLUG.get(tournament_slug, "NowTV")
