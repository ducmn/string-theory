"""Overlap and busy-time filtering.

Two passes:
1. Internal de-overlap — when two pushable matches overlap, keep only the
   highest-scored one. Greedy by score desc.
2. External busy-check — query the user's personal/work calendars via
   Google's freeBusy API and drop any match whose timeslot overlaps an
   existing busy interval. Service account needs read access on each
   busy calendar.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import Match

log = logging.getLogger(__name__)

DURATION_MINUTES = {
    "F": 180, "SF": 180, "QF": 180,
    "R16": 150, "R32": 120, "R64": 90, "R128": 90,
}
DEFAULT_DURATION_MIN = 120


def match_interval(m: Match) -> tuple[datetime, datetime]:
    start = m.start_utc
    end = start + timedelta(minutes=DURATION_MINUTES.get(m.round_short, DEFAULT_DURATION_MIN))
    return start, end


def _overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def pick_non_overlapping(matches: Iterable[Match]) -> list[Match]:
    """Greedy: highest-scored match wins on overlap.

    Tiebreaker: a match featuring a named favorite (favorite_bonus > 0) wins
    over one that doesn't. Without this, a top-5 headliner can shade a
    favorite-vs-other on equal numeric score — surprising to the user.
    """
    def sort_key(m: Match) -> tuple:
        fav_present = (m.score_breakdown or {}).get("favorite", 0.0) > 0
        return (-m.score, 0 if fav_present else 1, m.start_utc)

    by_score = sorted(matches, key=sort_key)
    kept: list[Match] = []
    intervals: list[tuple[datetime, datetime]] = []
    for m in by_score:
        iv = match_interval(m)
        if any(_overlaps(iv, e) for e in intervals):
            log.debug("dropping %s vs %s — overlaps higher-scored pick",
                      m.player_a.short_name, m.player_b.short_name)
            continue
        kept.append(m)
        intervals.append(iv)
    kept.sort(key=lambda m: m.start_utc)
    return kept


def fetch_busy_intervals(service, calendar_ids: list[str], time_min: datetime, time_max: datetime
                         ) -> list[tuple[datetime, datetime]]:
    """Single batched freeBusy.query for all given calendars."""
    body = {
        "timeMin": time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": [{"id": cid} for cid in calendar_ids],
    }
    res = service.freebusy().query(body=body).execute()
    out: list[tuple[datetime, datetime]] = []
    for cid, info in (res.get("calendars") or {}).items():
        errs = info.get("errors")
        if errs:
            log.warning("freeBusy error for %s: %s", cid, errs)
            continue
        for b in info.get("busy", []):
            try:
                s = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
                out.append((s, e))
            except Exception as ex:
                log.warning("could not parse busy interval %s: %s", b, ex)
    log.info("Busy intervals across %d calendars: %d", len(calendar_ids), len(out))
    return out


def filter_against_busy(matches: list[Match], busy: list[tuple[datetime, datetime]]) -> list[Match]:
    if not busy:
        return list(matches)
    out: list[Match] = []
    for m in matches:
        iv = match_interval(m)
        if any(_overlaps(iv, b) for b in busy):
            log.info("skip (busy conflict): %s vs %s @ %s",
                     m.player_a.short_name, m.player_b.short_name, m.start_utc.isoformat())
            continue
        out.append(m)
    return out


def busy_calendar_ids() -> list[str]:
    raw = os.environ.get("BUSY_CALENDAR_IDS", "").strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]
