"""Google Calendar idempotent upsert.

Event IDs are derived deterministically from (tournament, round, players) so
re-runs update an existing event in place rather than creating duplicates.
Google Calendar event IDs must be [a-v0-9]{5,1024} — we sha1-hash a readable
key (the readable form is logged for debugging).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .broadcaster import uk_broadcaster
from .models import Match

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

DURATION_MINUTES = {
    # Generous blocks — tennis matches routinely overrun. WTA / non-slam ATP
    # is best-of-3 (median ~100 min, tail to 3h+), slam men's best-of-5 can
    # exceed 5h. Better to over-block than have the calendar lie about
    # availability.
    "F": 300, "SF": 270, "QF": 240,
    "R16": 210, "R32": 180, "R64": 180, "R128": 120,
}
DEFAULT_DURATION_MIN = 180

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _player_slug(name: str) -> str:
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "'"):
            out.append("-")
    return "".join(out).strip("-") or "anon"


def legible_event_key(m: Match) -> str:
    """Human-readable identity used for hashing and debug logs."""
    pa, pb = sorted([m.player_a.full_name, m.player_b.full_name])
    return f"st-{m.tournament_slug}-{m.year}-{m.round_short.lower()}-{_player_slug(pa)}-{_player_slug(pb)}"


def calendar_event_id(m: Match) -> str:
    key = legible_event_key(m)
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"st{h}"


def _tournament_display(slug: str) -> str:
    return slug.replace("-", " ").title()


def event_title(m: Match) -> str:
    a = m.player_a.short_name or m.player_a.full_name
    b = m.player_b.short_name or m.player_b.full_name
    bcaster = uk_broadcaster(m.tournament_slug)
    return (
        f"{a} vs {b}, {_tournament_display(m.tournament_slug)} {m.round_short} "
        f"({m.surface}) — {bcaster}"
    )


def event_description(m: Match) -> str:
    bd = m.score_breakdown or {}
    breakdown_str = " + ".join(f"{k} {v}" for k, v in bd.items() if k != "total")
    rank_a = m.player_a.ranking or "NR"
    rank_b = m.player_b.ranking or "NR"
    return "\n".join([
        f"📺 Watch (UK): {uk_broadcaster(m.tournament_slug)}",
        f"📊 Live score: https://www.sofascore.com/event/{m.sofa_id}",
        "",
        f"{m.tournament_name} ({m.tournament_tier}, {m.round_name}, {m.surface})",
        f"{m.player_a.full_name} (#{rank_a}, {m.player_a.country_code}) "
        f"vs {m.player_b.full_name} (#{rank_b}, {m.player_b.country_code})",
        "",
        f"Score: {bd.get('total', m.score)} = {breakdown_str}",
        "",
        f"key: {legible_event_key(m)}",
    ])


def _duration(round_short: str) -> timedelta:
    return timedelta(minutes=DURATION_MINUTES.get(round_short, DEFAULT_DURATION_MIN))


def match_to_event(m: Match) -> dict:
    start = m.start_utc.astimezone(LONDON)
    end = start + _duration(m.round_short)
    return {
        "id": calendar_event_id(m),
        "summary": event_title(m),
        # Location field shows up in Google Calendar's compact agenda view
        # next to the event time, so the broadcaster is visible at a glance
        # without expanding the event.
        "location": uk_broadcaster(m.tournament_slug),
        "description": event_description(m),
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/London"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/London"},
        "source": {
            "title": "Sofascore live",
            "url": f"https://www.sofascore.com/event/{m.sofa_id}",
        },
    }


def _build_service(service_account_json: str):
    info = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _load_service_account_json() -> str:
    env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if env:
        if env.lstrip().startswith("{"):
            return env
        with open(env, "r", encoding="utf-8") as f:
            return f.read()
    fallback = os.path.join(os.getcwd(), "service-account.json")
    if os.path.exists(fallback):
        with open(fallback, "r", encoding="utf-8") as f:
            return f.read()
    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON env var not set and service-account.json not found."
    )


def build_calendar_service():
    """Build an authed Google Calendar service from env-configured credentials."""
    return _build_service(_load_service_account_json())


def prune_orphans(service, calendar_id: str, keep_event_ids: set[str],
                  time_min, time_max, dry_run: bool = False) -> int:
    """Delete previously-pushed events in [time_min, time_max] no longer in selection.

    Identifies events created by this tool by the `st`-prefix on the event ID
    (we hash with sha1 — all IDs match `st[0-9a-f]{40}`). Anything else is
    left alone. Pass `dry_run=True` to log without actually deleting.
    """
    deleted = 0
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat().replace("+00:00", "Z"),
            timeMax=time_max.isoformat().replace("+00:00", "Z"),
            singleEvents=True,
            pageToken=page_token,
            maxResults=2500,
        ).execute()
        for ev in resp.get("items", []):
            eid = ev.get("id", "")
            if not eid.startswith("st") or len(eid) < 40:
                continue
            if eid in keep_event_ids:
                continue
            if dry_run:
                log.info("[dry-run] would delete orphan %s  %s", eid, ev.get("summary", ""))
            else:
                try:
                    service.events().delete(calendarId=calendar_id, eventId=eid).execute()
                    log.info("deleted orphan %s  %s", eid, ev.get("summary", ""))
                except HttpError as e:
                    log.warning("delete failed for %s: %s", eid, e)
                    continue
            deleted += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return deleted


def upsert_matches(matches: Iterable[Match], calendar_id: str | None = None,
                   dry_run: bool = False, service=None) -> dict:
    """Upsert each match into the target Google Calendar.

    Returns counters: {created, updated, skipped, errors}.
    """
    counters = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    if dry_run:
        for m in matches:
            ev = match_to_event(m)
            log.info("[dry-run] %s  %s  score=%s", ev["start"]["dateTime"], ev["summary"], m.score)
            counters["skipped"] += 1
        return counters

    calendar_id = calendar_id or os.environ.get("TARGET_CALENDAR_ID")
    if not calendar_id:
        raise RuntimeError("TARGET_CALENDAR_ID not set (env var or argument).")

    if service is None:
        service = build_calendar_service()
    events = service.events()

    for m in matches:
        body = match_to_event(m)
        eid = body["id"]
        try:
            events.update(calendarId=calendar_id, eventId=eid, body=body).execute()
            log.info("updated %s  %s", eid, body["summary"])
            counters["updated"] += 1
        except HttpError as e:
            if e.resp.status == 404:
                try:
                    events.insert(calendarId=calendar_id, body=body).execute()
                    log.info("created %s  %s", eid, body["summary"])
                    counters["created"] += 1
                except HttpError as e2:
                    log.error("insert failed for %s: %s", eid, e2)
                    counters["errors"] += 1
            else:
                log.error("update failed for %s: %s", eid, e)
                counters["errors"] += 1

    return counters
