"""Daily entry point.

Pipeline:
    fetch  ─► score  ─► filter (threshold + watch window)
                       └─► de-overlap (highest score wins)
                              └─► drop conflicts vs personal/work calendars (freeBusy)
                                     └─► upsert to Tennis calendar
                                            └─► prune orphans no longer in selection

Usage:
    python -m string_theory.main           # push to calendar
    python -m string_theory.main --dry-run # print what would be pushed
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import time as dtime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .calendar_push import (
    build_calendar_service,
    calendar_event_id,
    prune_orphans,
    upsert_matches,
)
from .conflicts import (
    busy_calendar_ids,
    busy_ics_urls,
    fetch_busy_intervals,
    fetch_ics_busy_intervals,
    pick_non_overlapping,
    split_matches_around_busy,
    stack_sequential_matches,
)
from .models import Match
from .score import is_pushable, score_match
from .scrape import (
    SofascoreUnavailable,
    fetch_event_venue,
    fetch_upcoming_football_matches,
    fetch_upcoming_matches,
)

log = logging.getLogger("string_theory")

LONDON = ZoneInfo("Europe/London")
WATCH_WINDOW_START = dtime(7, 0)    # inclusive, London local
WATCH_WINDOW_END = dtime(23, 0)     # exclusive — a match starting past 11pm is too late


def is_in_watch_window(m: Match) -> bool:
    """True if the match's *start* is between 07:00 (incl.) and 23:00 (excl.)
    Europe/London. A match starting past 11pm is too late to bother with."""
    local = m.start_utc.astimezone(LONDON).time()
    return WATCH_WINDOW_START <= local < WATCH_WINDOW_END


def _with_court(m: Match) -> Match:
    """Attach the venue + city (best-effort) to a match for display."""
    from dataclasses import replace
    venue = fetch_event_venue(m.sofa_id)
    return replace(m, court=venue) if venue else m


def select_matches(matches: Iterable[Match]) -> list[Match]:
    out: list[Match] = []
    for m in matches:
        scored = score_match(m)
        if not is_pushable(scored):
            continue
        if not is_in_watch_window(scored):
            continue
        out.append(scored)
    out.sort(key=lambda x: x.start_utc)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push watch-worthy ATP/WTA matches to Google Calendar.")
    parser.add_argument("--dry-run", action="store_true", help="Print the schedule, don't write to calendar.")
    parser.add_argument("--days-ahead", type=int, default=5,
                        help="How many days of upcoming matches to fetch (default 5 — covers a typical week of relevant events).")
    parser.add_argument("--calendar-id", default=None, help="Override TARGET_CALENDAR_ID env var.")
    parser.add_argument("--all", action="store_true", help="Don't filter — score everything and dump (debugging).")
    parser.add_argument("--no-prune", action="store_true", help="Skip orphan deletion pass.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    # Track whether Sofascore actually answered. An empty result is the NORMAL
    # state now that scope is Wimbledon + World Cup, so "no matches" must not
    # be mistaken for an outage — otherwise stale events could never be pruned.
    source_ok = False
    try:
        tennis = fetch_upcoming_matches(days_ahead=args.days_ahead)
        source_ok = True
    except SofascoreUnavailable as e:
        log.warning("tennis source unavailable: %s", e)
        tennis = []
    try:
        football = fetch_upcoming_football_matches(days_ahead=args.days_ahead)
        source_ok = True
    except SofascoreUnavailable as e:
        log.warning("football source unavailable: %s", e)
        football = []
    raw = tennis + football
    log.info("Fetched %d candidate matches (%d tennis + %d football)",
             len(raw), len(tennis), len(football))

    if args.all:
        scored = sorted((score_match(m) for m in raw), key=lambda m: -m.score)
        for m in scored[:30]:
            log.info("score=%4.1f  %s  %s vs %s  (%s, %s)",
                     m.score,
                     m.start_utc.astimezone(LONDON).strftime("%a %H:%M"),
                     m.player_a.short_name, m.player_b.short_name,
                     m.tournament_slug, m.round_short)
        return 0

    pushable = select_matches(raw)
    log.info("Pushable after score+window: %d", len(pushable))

    calendar_id = args.calendar_id or os.environ.get("TARGET_CALENDAR_ID")
    service = None

    # Pull the user's existing commitments (personal Google + work ICS) so we
    # can cut matches short / split them around real events. Our own pushed
    # events are excluded from the busy set, so no self-conflict.
    # Personal (Google) and work (ICS/Outlook) busy are kept separate: a
    # favorite match runs over the top of personal commitments but must YIELD
    # to the work calendar — the user won't watch even Dimitrov over a work
    # meeting.
    busy_ids = busy_calendar_ids()
    ics_urls = busy_ics_urls()
    busy: list = []          # personal — favorites exempt
    busy_work: list = []     # work — favorites yield too
    if (busy_ids or ics_urls) and not args.dry_run and pushable:
        time_min = min(m.start_utc for m in pushable) - timedelta(minutes=30)
        time_max = max(m.start_utc for m in pushable) + timedelta(hours=6)
        if busy_ids:
            service = build_calendar_service()
            busy.extend(fetch_busy_intervals(service, busy_ids, time_min, time_max))
        if ics_urls:
            busy_work.extend(fetch_ics_busy_intervals(ics_urls, time_min, time_max))
    elif (busy_ids or ics_urls) and args.dry_run:
        log.info("[dry-run] would cut/split matches around events on %d google + %d ICS feeds",
                 len(busy_ids), len(ics_urls))

    # De-overlap competing matches (favorites first, then tennis over football,
    # then score).
    deduped = pick_non_overlapping(pushable)
    # Back-to-back matches at the same tournament (e.g. two Wimbledon semis on
    # Centre Court) are sequential — stack them so each keeps a full block and
    # later ones start when the previous finishes (no overlap).
    deduped = stack_sequential_matches(deduped)
    # Against the user's real calendar: don't drop a clashing match — cut it
    # short, and if the clash is mid-match, split it so they can resume after.
    # Favorites are exempt from PERSONAL busy (they run over the top) but still
    # yield to the WORK calendar. Uses the final stacked positions.
    if busy or busy_work:
        deduped = split_matches_around_busy(deduped, busy, work_busy=busy_work)
    # Enrich the final selection with court/venue (per-event call — cheap for
    # this handful). Best-effort; matches without a court are left as-is.
    deduped = [_with_court(m) for m in deduped]
    log.info("Final events after cut/split: %d", len(deduped))

    # Safety: only skip the calendar update when Sofascore was unreachable.
    # An empty selection with a HEALTHY source is normal (Wimbledon and the
    # World Cup are on for a few weeks a year) and must still prune, so that
    # events which no longer qualify get deleted.
    if not source_ok:
        log.warning("Sofascore unreachable — skipping calendar update entirely (no prune).")
        return 0

    counters = upsert_matches(deduped, calendar_id=calendar_id, dry_run=args.dry_run, service=service)

    if not args.dry_run and not args.no_prune and calendar_id:
        if service is None:
            service = build_calendar_service()
        keep_ids = {calendar_event_id(m) for m in deduped}
        # Prune orphans within the same window we fetched (3 days from today UTC).
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        time_min = now - timedelta(hours=12)
        time_max = now + timedelta(days=args.days_ahead + 1)
        pruned = prune_orphans(service, calendar_id, keep_ids, time_min, time_max)
        counters["pruned"] = pruned

    log.info("Done: %s", counters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
