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
    filter_against_busy,
    pick_non_overlapping,
)
from .models import Match
from .score import is_pushable, score_match
from .scrape import fetch_upcoming_matches

log = logging.getLogger("string_theory")

LONDON = ZoneInfo("Europe/London")
WATCH_WINDOW_START = dtime(7, 0)    # inclusive, London local
WATCH_WINDOW_END = dtime(22, 30)    # exclusive — past this, the user's asleep

# User is in the office on these weekdays (Monday=0); daytime tennis is
# blacked out regardless of score or specific calendar conflicts.
OFFICE_DAYS = {1, 3}                # Tue, Thu
OFFICE_HOURS_START = dtime(9, 0)
OFFICE_HOURS_END = dtime(18, 0)


def is_in_watch_window(m: Match) -> bool:
    """True if the match's *start* is between 07:00 (incl.) and 22:30 (excl.)
    Europe/London. Past 22:30 we don't bother — even a short block would push
    into bedtime."""
    local = m.start_utc.astimezone(LONDON).time()
    return WATCH_WINDOW_START <= local < WATCH_WINDOW_END


def is_in_office_hours(m: Match) -> bool:
    """True if the match falls during the user's in-office window (Tue/Thu
    09:00–18:00 London). Used to blanket-skip daytime matches on those days
    without relying on individual calendar events."""
    local_dt = m.start_utc.astimezone(LONDON)
    if local_dt.weekday() not in OFFICE_DAYS:
        return False
    return OFFICE_HOURS_START <= local_dt.time() < OFFICE_HOURS_END


def select_matches(matches: Iterable[Match]) -> list[Match]:
    out: list[Match] = []
    for m in matches:
        scored = score_match(m)
        if not is_pushable(scored):
            continue
        if not is_in_watch_window(scored):
            continue
        if is_in_office_hours(scored):
            log.info("skip (office day): %s vs %s @ %s",
                     scored.player_a.short_name, scored.player_b.short_name,
                     scored.start_utc.astimezone(LONDON).strftime("%a %H:%M"))
            continue
        out.append(scored)
    out.sort(key=lambda x: x.start_utc)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push watch-worthy ATP/WTA matches to Google Calendar.")
    parser.add_argument("--dry-run", action="store_true", help="Print the schedule, don't write to calendar.")
    parser.add_argument("--days-ahead", type=int, default=2, help="How many days of upcoming matches to fetch.")
    parser.add_argument("--calendar-id", default=None, help="Override TARGET_CALENDAR_ID env var.")
    parser.add_argument("--all", action="store_true", help="Don't filter — score everything and dump (debugging).")
    parser.add_argument("--no-prune", action="store_true", help="Skip orphan deletion pass.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    raw = fetch_upcoming_matches(days_ahead=args.days_ahead)
    log.info("Fetched %d candidate matches", len(raw))

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

    deduped = pick_non_overlapping(pushable)
    log.info("After internal de-overlap: %d", len(deduped))

    calendar_id = args.calendar_id or os.environ.get("TARGET_CALENDAR_ID")

    service = None
    busy_ids = busy_calendar_ids()
    ics_urls = busy_ics_urls()
    if (busy_ids or ics_urls) and not args.dry_run and deduped:
        time_min = min(m.start_utc for m in deduped) - timedelta(minutes=30)
        time_max = max(m.start_utc for m in deduped) + timedelta(hours=6)
        busy: list = []
        if busy_ids:
            service = build_calendar_service()
            busy.extend(fetch_busy_intervals(service, busy_ids, time_min, time_max))
        if ics_urls:
            busy.extend(fetch_ics_busy_intervals(ics_urls, time_min, time_max))
        deduped = filter_against_busy(deduped, busy)
        log.info("After busy-calendar filter: %d", len(deduped))
    elif (busy_ids or ics_urls) and args.dry_run:
        log.info("[dry-run] would query busy on %d google + %d ICS feeds", len(busy_ids), len(ics_urls))

    if not deduped and not args.no_prune:
        log.info("Nothing pushable — skipping calendar update entirely (no prune).")
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
