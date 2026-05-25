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
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .calendar_push import LONDON, _clip_to_bedtime, duration_minutes
from .models import Match

log = logging.getLogger(__name__)


def match_interval(m: Match) -> tuple[datetime, datetime]:
    start = m.start_utc
    start_local = start.astimezone(LONDON)
    raw_end_local = start_local + timedelta(minutes=duration_minutes(m))
    end = _clip_to_bedtime(start_local, raw_end_local).astimezone(start.tzinfo)
    return start, end


def _overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# Threshold for "these two matches are effectively the same slot" in dedup.
# Below this, a tail/start overlap is tolerated and both matches are kept.
HEAVY_OVERLAP_RATIO = 0.5


def _is_heavy_overlap(a: tuple[datetime, datetime],
                     b: tuple[datetime, datetime],
                     ratio: float = HEAVY_OVERLAP_RATIO) -> bool:
    """True if a and b overlap by at least `ratio` of the SHORTER match's
    duration. Lets a 90-min match whose tail clips another match's start
    coexist (small overlap), while still de-duping near-identical slots."""
    overlap_start = max(a[0], b[0])
    overlap_end = min(a[1], b[1])
    if overlap_end <= overlap_start:
        return False
    overlap_secs = (overlap_end - overlap_start).total_seconds()
    shorter_secs = min((a[1] - a[0]).total_seconds(), (b[1] - b[0]).total_seconds())
    return shorter_secs > 0 and overlap_secs / shorter_secs >= ratio


def pick_non_overlapping(matches: Iterable[Match]) -> list[Match]:
    """Greedy: favorite matches always win on overlap; among non-favorites,
    highest-scored wins. Final tiebreaker is start time.

    A match featuring a named favorite (favorite_bonus > 0) sorts ahead of
    every non-favorite regardless of score difference — the user has
    explicitly said favorites matter more than headline scores. With one
    favorite (Learner Tien) this is rare, but when it happens we don't
    want a higher-scored Auger-Aliassime to evict a Tien match.
    """
    def sort_key(m: Match) -> tuple:
        fav_present = (m.score_breakdown or {}).get("favorite", 0.0) > 0
        return (0 if fav_present else 1, -m.score, m.start_utc)

    by_score = sorted(matches, key=sort_key)
    kept: list[Match] = []
    intervals: list[tuple[datetime, datetime]] = []
    for m in by_score:
        iv = match_interval(m)
        if any(_is_heavy_overlap(iv, e) for e in intervals):
            log.debug("dropping %s vs %s — heavily overlaps higher-scored pick",
                      m.player_a.short_name, m.player_b.short_name)
            continue
        kept.append(m)
        intervals.append(iv)
    kept.sort(key=lambda m: m.start_utc)
    return kept


def fetch_busy_intervals(service, calendar_ids: list[str], time_min: datetime, time_max: datetime
                         ) -> list[tuple[datetime, datetime]]:
    """Pull busy intervals from each Google calendar via `events.list`.

    Uses events.list (not freeBusy) so we get event titles — needed to apply
    BUSY_EXCEPTIONS like 'improv show on Sat = 4–6pm is actually free'.
    """
    out: list[tuple[datetime, datetime]] = []
    time_min_str = time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    time_max_str = time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    for cid in calendar_ids:
        try:
            page_token = None
            events: list[tuple[str, datetime, datetime]] = []
            while True:
                resp = service.events().list(
                    calendarId=cid,
                    timeMin=time_min_str,
                    timeMax=time_max_str,
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                    maxResults=2500,
                    fields="items(summary,start,end,status,transparency),nextPageToken",
                ).execute()
                for ev in resp.get("items", []):
                    if ev.get("status") == "cancelled":
                        continue
                    if ev.get("transparency") == "transparent":  # "Free" events
                        continue
                    s = _parse_event_dt(ev.get("start"))
                    e = _parse_event_dt(ev.get("end"))
                    if not s or not e:
                        continue
                    events.append((ev.get("summary", "") or "", s, e))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            out.extend(apply_busy_exceptions(events))
        except Exception as ex:
            log.warning("events.list failed for %s: %s", cid, ex)
    log.info("Busy intervals across %d calendars: %d", len(calendar_ids), len(out))
    return out


def _parse_event_dt(slot: dict | None) -> datetime | None:
    if not slot:
        return None
    raw = slot.get("dateTime") or slot.get("date")
    if not raw:
        return None
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return datetime.fromisoformat(raw + "T00:00:00+00:00")
    except ValueError:
        return None


# --- Busy exceptions --------------------------------------------------------

# Each entry: {title_pattern: compiled re, weekday: int (Mon=0), free_local: (time, time)}.
# Matching events get sliced so the free_local window is treated as available.
# Empty by default; populate with rules like:
#   import re
#   from datetime import time
#   BUSY_EXCEPTIONS = [
#       {"title_pattern": re.compile(r"yoga", re.I),
#        "weekday": 1,  # Tue
#        "free_local": (time(18, 0), time(19, 0))},
#   ]
BUSY_EXCEPTIONS: list[dict] = []


def apply_busy_exceptions(events: list[tuple[str, datetime, datetime]]
                          ) -> list[tuple[datetime, datetime]]:
    """Convert (title, start, end) events to busy intervals, excising the
    'free_local' window for any event whose title and weekday match a rule."""
    out: list[tuple[datetime, datetime]] = []
    for title, start, end in events:
        rule = _match_exception(title, start)
        if rule is None:
            out.append((start, end))
            continue
        free_s, free_e = _local_window_to_utc(start, rule["free_local"])
        if start < free_s:
            out.append((start, min(free_s, end)))
        if free_e < end:
            out.append((max(free_e, start), end))
        log.info("Applied exception %r to %s — split around %s–%s",
                 rule["title_pattern"].pattern, title, free_s.isoformat(), free_e.isoformat())
    return out


def _match_exception(title: str, start_utc: datetime) -> dict | None:
    if not title:
        return None
    local_dow = start_utc.astimezone(LONDON).weekday()
    for rule in BUSY_EXCEPTIONS:
        if rule["weekday"] != local_dow:
            continue
        if rule["title_pattern"].search(title):
            return rule
    return None


def _local_window_to_utc(reference_utc: datetime,
                          free_local: tuple[dtime, dtime]
                          ) -> tuple[datetime, datetime]:
    local_date = reference_utc.astimezone(LONDON).date()
    s_local = datetime.combine(local_date, free_local[0]).replace(tzinfo=LONDON)
    e_local = datetime.combine(local_date, free_local[1]).replace(tzinfo=LONDON)
    return s_local.astimezone(timezone.utc), e_local.astimezone(timezone.utc)


MIN_FREE_MINUTES = 60  # below this remaining-free time, skip the match entirely


def _largest_free_segment(window: tuple[datetime, datetime],
                          busy: list[tuple[datetime, datetime]]
                          ) -> tuple[datetime, datetime] | None:
    """Subtract busy intervals from `window`, return the largest free chunk."""
    free: list[tuple[datetime, datetime]] = [window]
    for bs, be in busy:
        new_free: list[tuple[datetime, datetime]] = []
        for fs, fe in free:
            if be <= fs or bs >= fe:
                new_free.append((fs, fe))
                continue
            if fs < bs:
                new_free.append((fs, bs))
            if be < fe:
                new_free.append((be, fe))
        free = new_free
    return max(free, key=lambda x: x[1] - x[0]) if free else None


def filter_fully_subsumed(matches: list[Match],
                          busy: list[tuple[datetime, datetime]]) -> list[Match]:
    """Drop a match ONLY if its entire window is covered by busy time.

    Conservative conflict check: any free gap at all (start, middle, or
    end) keeps the match, pushed with its full natural block — no
    clipping. A match is dropped only when the user is busy for 100% of
    its window (union of all busy intervals fully subsumes it).
    """
    if not busy:
        return list(matches)
    out: list[Match] = []
    for m in matches:
        iv = match_interval(m)
        free = _largest_free_segment(iv, busy)
        if free is None or free[1] <= free[0]:
            log.info("skip (fully busy): %s vs %s @ %s",
                     m.player_a.short_name, m.player_b.short_name, m.start_utc.isoformat())
            continue
        out.append(m)
    return out


def filter_against_busy(matches: list[Match],
                        busy: list[tuple[datetime, datetime]],
                        min_free_minutes: int = MIN_FREE_MINUTES) -> list[Match]:
    """For each match, intersect its natural window with busy intervals.

    - If the match window has no overlap, keep it as-is.
    - If a partial overlap leaves a contiguous free chunk of at least
      `min_free_minutes`, push the match clipped to that free chunk
      (handles cases like a 14:00–14:30 meeting at the start of a 3h
      match — push 14:30–17:00 instead of dropping the whole thing).
    - If the largest free chunk is below threshold, drop the match.
    """
    from dataclasses import replace as _replace

    if not busy:
        return list(matches)
    out: list[Match] = []
    for m in matches:
        iv = match_interval(m)
        free = _largest_free_segment(iv, busy)
        if free is None or (free[1] - free[0]).total_seconds() / 60 < min_free_minutes:
            log.info("skip (busy conflict): %s vs %s @ %s",
                     m.player_a.short_name, m.player_b.short_name, m.start_utc.isoformat())
            continue
        if free != iv:
            log.info("clip (busy partial): %s vs %s — pushing %s–%s instead of full block",
                     m.player_a.short_name, m.player_b.short_name,
                     free[0].astimezone(LONDON).strftime("%H:%M"),
                     free[1].astimezone(LONDON).strftime("%H:%M"))
            m = _replace(m, event_clip_start_utc=free[0], event_clip_end_utc=free[1])
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


# Events longer than this are treated as vacation/OOO/long-range blocks and
# excluded from busy filtering — a 12-day "Out of Office" shouldn't pre-empt
# every evening tennis match in that span.
ICS_MAX_BUSY_HOURS = 18


def fetch_ics_busy_intervals(urls: list[str], time_min: datetime, time_max: datetime
                             ) -> list[tuple[datetime, datetime]]:
    """Pull DTSTART/DTEND from each ICS URL, return events overlapping the window.

    Outlook/Exchange "Publish a calendar" URLs return ICS feeds. The format
    is RFC 5545. Logic:
    - Skip all-day events (DATE-only DTSTART) — usually vacation markers.
    - Skip events longer than ICS_MAX_BUSY_HOURS — a multi-day OOO event
      is not the kind of "busy" that should pre-empt a 2h tennis slot.
    - Floating times interpreted as Europe/London.
    - Recurrence rules are not expanded (best-effort stdlib parser).

    Stdlib-only by design — no icalendar pip dep — to keep the artefact small.
    """
    import urllib.request

    out: list[tuple[datetime, datetime]] = []
    skipped_allday = 0
    skipped_long = 0

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
            if _is_allday(block, "DTSTART"):
                skipped_allday += 1
                continue
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
            if (end - start).total_seconds() > ICS_MAX_BUSY_HOURS * 3600:
                skipped_long += 1
                continue
            out.append((start, end))

    log.info("ICS busy intervals across %d feeds: %d  (skipped %d all-day, %d multi-day)",
             len(urls), len(out), skipped_allday, skipped_long)
    return out


def _is_allday(block: str, key: str) -> bool:
    """RFC 5545 all-day events use VALUE=DATE on DTSTART, no time component."""
    pattern = rf"\n{key}(;[^:\n]*)?:([^\r\n]+)"
    m = re.search(pattern, "\n" + block)
    if not m:
        return False
    params = m.group(1) or ""
    raw = m.group(2).strip()
    if "VALUE=DATE" in params and "VALUE=DATE-TIME" not in params:
        return True
    return "T" not in raw  # YYYYMMDD without time


def _parse_ics_dt(block: str, key: str, default_tz):
    """Pull DTSTART/DTEND value from a VEVENT block. Return tz-aware datetime."""
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
