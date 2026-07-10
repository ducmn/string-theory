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

from .calendar_push import LONDON, _clip_end, duration_minutes
from .models import Match

log = logging.getLogger(__name__)


def match_interval(m: Match) -> tuple[datetime, datetime]:
    # If the event was shifted/clipped (e.g. stacked after an earlier match on
    # the same court), use that actual window — overlap and dedup math must see
    # where the block really lands, not its nominal start.
    if m.event_clip_start_utc and m.event_clip_end_utc:
        return m.event_clip_start_utc, m.event_clip_end_utc
    start = m.start_utc
    start_local = start.astimezone(LONDON)
    raw_end_local = start_local + timedelta(minutes=duration_minutes(m))
    # Favorite matches (incl. England) run past bedtime, so their interval
    # reflects the full block here too — used for overlap/dedup math.
    end = _clip_end(start_local, raw_end_local, m).astimezone(start.tzinfo)
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
        # Priority: favorites first, then tennis over football (the user
        # prefers tennis when the two clash, even though football scores
        # higher), then by score, then earliest start.
        is_football = m.tour == "football"
        return (0 if fav_present else 1, 1 if is_football else 0, -m.score, m.start_utc)

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


def stack_sequential_matches(matches: list[Match]) -> list[Match]:
    """Same-tournament matches share the show court and are played back-to-back.
    Each keeps its FULL block; a later match's listed start time is only a "not
    before" — it really begins when the previous one finishes. So we stack
    them: give the first match its natural block and push each subsequent
    same-tournament match's start to when the previous block ends. Result:
    realistic per-match durations AND no overlap.

    e.g. two Wimbledon men's semis on Centre Court, each a ~2h45 block:
    13:30–16:15 (Fery), then 16:15–19:00 (Sinner) instead of squashing the
    first into 13:30–15:10. Different tournaments are left alone (separate
    courts / channels can run concurrently)."""
    from dataclasses import replace as _replace
    from collections import defaultdict

    out = list(matches)
    groups: dict = defaultdict(list)
    for i, m in enumerate(out):
        groups[m.tournament_slug].append(i)

    for _slug, idxs in groups.items():
        idxs.sort(key=lambda i: out[i].start_utc)
        cursor = None  # UTC datetime the show court next frees up
        for i in idxs:
            m = out[i]
            if cursor is not None and cursor > m.start_utc:
                # Court still busy at this match's listed start → push it back.
                new_start = cursor
                raw_end = new_start + timedelta(minutes=duration_minutes(m))
                new_end = _clip_end(new_start.astimezone(LONDON),
                                    raw_end.astimezone(LONDON),
                                    m).astimezone(new_start.tzinfo)
                out[i] = _replace(m, event_clip_start_utc=new_start,
                                  event_clip_end_utc=new_end)
                cursor = new_end
            else:
                # First (or non-overlapping) match keeps its natural block.
                _, cursor = match_interval(m)
    return out


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
                    fields="items(id,summary,start,end,status,transparency),nextPageToken",
                ).execute()
                for ev in resp.get("items", []):
                    if ev.get("status") == "cancelled":
                        continue
                    # Skip all-day (date-only) events — vacation/birthday
                    # markers shouldn't blanket-block evening matches. A TIMED
                    # event counts as a clash even if it's marked "Free"
                    # (the user wants matches kept off timed Free events like a
                    # read-only "fromGmail" reservation that can't be flipped
                    # to Busy via the API).
                    if not (ev.get("start") or {}).get("dateTime"):
                        continue
                    # Skip our own pushed events (id = "st"+sha1). Otherwise,
                    # when the target calendar is also a busy source (e.g.
                    # TARGET_CALENDAR_ID=primary), a match would count its own
                    # event as "busy", get dropped as fully-subsumed, pruned,
                    # then re-added next run — an hourly flap.
                    eid = ev.get("id", "")
                    if eid.startswith("st") and len(eid) >= 40:
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


def _free_segments(window: tuple[datetime, datetime],
                   busy: list[tuple[datetime, datetime]]
                   ) -> list[tuple[datetime, datetime]]:
    """Subtract busy intervals from `window`; return all free chunks, in time
    order. A busy interval in the middle yields two chunks (before / after)."""
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
    return sorted(free, key=lambda x: x[0])


def _largest_free_segment(window: tuple[datetime, datetime],
                          busy: list[tuple[datetime, datetime]]
                          ) -> tuple[datetime, datetime] | None:
    """Subtract busy intervals from `window`, return the largest free chunk."""
    free = _free_segments(window, busy)
    return max(free, key=lambda x: x[1] - x[0]) if free else None


def split_matches_around_busy(matches: list[Match],
                              busy: list[tuple[datetime, datetime]],
                              min_free_minutes: int = MIN_FREE_MINUTES) -> list[Match]:
    """Instead of dropping a match that overlaps an existing event, CUT IT
    SHORT — and if the conflict is in the middle, split it so the user can
    RESUME after. Each qualifying free segment (>= min_free_minutes) becomes
    its own event (part=1, part=2 "resume", ...). A segment shorter than the
    floor is discarded; if nothing qualifies the match drops out entirely.

    Favorites (a favorite player, or England) are must-watch and are never
    cut or split — they run their full block over the top of anything."""
    from dataclasses import replace as _replace

    if not busy:
        return list(matches)
    out: list[Match] = []
    for m in matches:
        if (m.score_breakdown or {}).get("favorite", 0.0) > 0:
            out.append(m)
            continue
        iv = match_interval(m)
        segs = [s for s in _free_segments(iv, busy)
                if (s[1] - s[0]).total_seconds() / 60 >= min_free_minutes]
        if not segs:
            log.info("skip (busy conflict): %s vs %s @ %s",
                     m.player_a.short_name, m.player_b.short_name, m.start_utc.isoformat())
            continue
        if len(segs) == 1 and segs[0] == iv:
            out.append(m)  # no conflict — full block
            continue
        for idx, (s, e) in enumerate(segs, start=1):
            tag = "resume" if idx > 1 else "cut short"
            log.info("%s (busy): %s vs %s — part %d %s–%s",
                     tag, m.player_a.short_name, m.player_b.short_name, idx,
                     s.astimezone(LONDON).strftime("%H:%M"), e.astimezone(LONDON).strftime("%H:%M"))
            out.append(_replace(m, event_clip_start_utc=s, event_clip_end_utc=e, part=idx))
    return out


def filter_no_overlap(matches: list[Match],
                      busy: list[tuple[datetime, datetime]]) -> list[Match]:
    """Drop a match if its window overlaps ANY busy interval at all.

    Stricter than filter_fully_subsumed: the user doesn't want a match
    written onto the calendar if it clashes with an existing event even
    partially. (Our own pushed events are excluded upstream in
    fetch_busy_intervals, so a match never conflicts with itself.)
    """
    if not busy:
        return list(matches)
    out: list[Match] = []
    for m in matches:
        # A favorite (a favorite player, or England) is must-watch: never drop
        # it for clashing with an existing event — the user has said they'll
        # watch it anyway (and stay up past bedtime for it).
        if (m.score_breakdown or {}).get("favorite", 0.0) > 0:
            out.append(m)
            continue
        iv = match_interval(m)
        clash = next((b for b in busy if _overlaps(iv, b)), None)
        if clash is not None:
            log.info("skip (overlaps existing event %s–%s): %s vs %s @ %s",
                     clash[0].astimezone(LONDON).strftime("%a %H:%M"),
                     clash[1].astimezone(LONDON).strftime("%H:%M"),
                     m.player_a.short_name, m.player_b.short_name,
                     m.start_utc.astimezone(LONDON).strftime("%a %H:%M"))
            continue
        out.append(m)
    return out


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
        # Favorites (a favorite player, or England) are must-watch: never
        # clipped or dropped for a conflict — they run their full block.
        if (m.score_breakdown or {}).get("favorite", 0.0) > 0:
            out.append(m)
            continue
        iv = match_interval(m)
        free = _largest_free_segment(iv, busy)
        if free is None or (free[1] - free[0]).total_seconds() / 60 < min_free_minutes:
            log.info("skip (busy conflict): %s vs %s @ %s",
                     m.player_a.short_name, m.player_b.short_name, m.start_utc.isoformat())
            continue
        if free != iv:
            log.info("clip (busy partial): %s vs %s — cutting short to %s–%s instead of full block",
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
