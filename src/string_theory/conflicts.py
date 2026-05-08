"""Overlap and busy-time filtering.

Two passes:
1. Internal de-overlap — when two pushable matches overlap, keep only the
   highest-scored one. Greedy by score desc.
2. External busy-check — drop any match whose timeslot overlaps an existing
   busy interval pulled from:
   - Google freeBusy on the calendars listed in BUSY_CALENDAR_IDS
     (service account needs read access on each).
   - Plain ICS feeds listed in BUSY_ICS_URLS, e.g. an Outlook/Exchange
     "Publish a calendar" URL. Useful when the work calendar is on
     Office365/Exchange and isn't reachable via Google's API.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .calendar_push import DEFAULT_DURATION_MIN, DURATION_MINUTES
from .models import Match

log = logging.getLogger(__name__)


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


def busy_ics_urls() -> list[str]:
    raw = os.environ.get("BUSY_ICS_URLS", "").strip()
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def fetch_ics_busy_intervals(urls: list[str], time_min: datetime, time_max: datetime
                             ) -> list[tuple[datetime, datetime]]:
    """Pull DTSTART/DTEND from each ICS URL, return events overlapping the window.

    Outlook/Exchange "Publish a calendar" URLs return ICS feeds. The format
    is RFC 5545. We only care about VEVENT blocks with DTSTART/DTEND;
    floating times are interpreted as Europe/London (the user's local TZ).
    All-day events (DATE-only) and recurring events that don't expand are
    handled best-effort — the heuristic-level recurrence handling here is
    deliberately limited; if the user finds it lossy, switch to icalendar.

    Implementing this with stdlib only (no icalendar pip dep) keeps the
    deployment artefact small.
    """
    import urllib.request
    from zoneinfo import ZoneInfo

    LONDON = ZoneInfo("Europe/London")
    out: list[tuple[datetime, datetime]] = []

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "string-theory/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning("ICS fetch failed for %s: %s", url, e)
            continue

        # Unfold long lines (RFC 5545 §3.1: continuation = CRLF + space/tab)
        unfolded = body.replace("\r\n ", "").replace("\r\n\t", "").replace("\n ", "").replace("\n\t", "")

        for raw_event in unfolded.split("BEGIN:VEVENT")[1:]:
            block = raw_event.split("END:VEVENT")[0]
            start = _parse_ics_dt(block, "DTSTART", LONDON)
            end = _parse_ics_dt(block, "DTEND", LONDON)
            if start is None or end is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=LONDON)
            if end.tzinfo is None:
                end = end.replace(tzinfo=LONDON)
            if end <= time_min or start >= time_max:
                continue
            out.append((start, end))

    log.info("ICS busy intervals across %d feeds: %d", len(urls), len(out))
    return out


def _parse_ics_dt(block: str, key: str, default_tz):
    """Pull DTSTART/DTEND value from a VEVENT block. Return tz-aware datetime."""
    import re
    from zoneinfo import ZoneInfo

    # Match "DTSTART:..." or "DTSTART;TZID=Europe/London:..." etc.
    pattern = rf"\n{key}(;[^:\n]*)?:([^\r\n]+)"
    m = re.search(pattern, "\n" + block)
    if not m:
        return None
    params = m.group(1) or ""
    raw = m.group(2).strip()

    tz = default_tz
    tzid_m = re.search(r"TZID=([^;:]+)", params)
    if tzid_m:
        try:
            tz = ZoneInfo(tzid_m.group(1))
        except Exception:
            pass

    raw = raw.rstrip("Z")
    is_utc = m.group(2).strip().endswith("Z")
    try:
        if "T" in raw:
            dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        else:
            dt = datetime.strptime(raw, "%Y%m%d")
        if is_utc:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        return None
