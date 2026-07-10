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
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .broadcaster import uk_broadcaster, uk_broadcaster_for_match
from .models import Match

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

# Cap every event end at 23:00 London — past that, the user is asleep.
# An 18:00 BST match runs to ~20:00 (fine); a 21:30 BST match would run past
# midnight, so we clip it to a 21:30–23:00 block.
WATCH_END_HOUR = 23
WATCH_END_MIN = 0


def _clip_to_bedtime(start_local, end_local):
    """Cap end_local at 23:00 on start_local's *date*. Caller guarantees
    start_local is already < 23:00 (enforced by the watch-window filter)."""
    cap = start_local.replace(hour=WATCH_END_HOUR, minute=WATCH_END_MIN, second=0, microsecond=0)
    return min(end_local, cap)


def _is_favorite_match(m) -> bool:
    """True if a named favorite (a favorite player, or England) is playing.
    These get to run past bedtime — see _clip_end."""
    return bool((getattr(m, "score_breakdown", None) or {}).get("favorite", 0.0) > 0)


def _clip_end(start_local, end_local, m):
    """Cap the event end at bedtime (23:00) — UNLESS a favorite is playing,
    in which case let the block run to its natural end so the user can watch
    to the finish ('if england game then can stay up a bit... adjust sleep')."""
    if _is_favorite_match(m):
        return end_local
    return _clip_to_bedtime(start_local, end_local)


# WTA is always best-of-3 (median ~100 min), so shorter blocks. ATP is
# best-of-3 at non-slams and best-of-5 at Grand Slams (median ~150 min,
# tail to 5h+); the ATP table is generous to cover both.
DURATION_MINUTES_WTA = {
    "F": 135, "SF": 120, "QF": 120,
    "R16": 105, "R32": 105, "R64": 105, "R128": 90,
}
DURATION_MINUTES_ATP = {
    "F": 180, "SF": 165, "QF": 150,
    "R16": 135, "R32": 120, "R64": 120, "R128": 105,
}
# Kept as the legacy default for code paths that don't know the tour
# (e.g. when something fails to populate Match.tour).
DURATION_MINUTES = DURATION_MINUTES_ATP
DEFAULT_DURATION_MIN = 180


def duration_minutes(m) -> int:
    """Pick the duration table by tour, fall back to ATP if unknown."""
    tour = getattr(m, "tour", None)
    if tour == "football":
        # 90 min match + ~20 pre-match + injury time + potentially ET+pens
        # in knockout rounds — block 2h30m gives the user a fair window.
        return 150
    table = DURATION_MINUTES_WTA if tour == "wta" else DURATION_MINUTES_ATP
    return table.get(m.round_short, DEFAULT_DURATION_MIN)

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
    # A split match's later ("resume") blocks need distinct, stable IDs. part=1
    # keeps the base key so existing single-block events update in place.
    if getattr(m, "part", 1) > 1:
        key = f"{key}-part{m.part}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"st{h}"


def _tournament_display(slug: str) -> str:
    return slug.replace("-", " ").title()


def event_title(m: Match) -> str:
    a = m.player_a.short_name or m.player_a.full_name
    b = m.player_b.short_name or m.player_b.full_name
    resume_prefix = "▶ resume: " if getattr(m, "part", 1) > 1 else ""
    court_suffix = f" · {m.court}" if m.court else ""
    return (f"{resume_prefix}{a} vs {b}, {_tournament_display(m.tournament_slug)} "
            f"{m.round_short}{court_suffix}")


def event_description(m: Match) -> str:
    rank_a = m.player_a.ranking or "NR"
    rank_b = m.player_b.ranking or "NR"
    # Round / court — omit the tier ("GS") and surface ("grass"); skip blanks.
    detail = ", ".join(x for x in (m.round_name, m.court) if x)
    return "\n".join([
        f"📊 Live score: https://www.sofascore.com/event/{m.sofa_id}",
        "",
        f"{m.tournament_name} ({detail})",
        f"{m.player_a.full_name} (#{rank_a}, {m.player_a.country_code}) "
        f"vs {m.player_b.full_name} (#{rank_b}, {m.player_b.country_code})",
    ])


def _duration(round_short: str) -> timedelta:
    # Legacy signature — kept tour-agnostic. Prefer _duration_for(m) when
    # you have a Match in hand so the tour-aware table is consulted.
    return timedelta(minutes=DURATION_MINUTES_ATP.get(round_short, DEFAULT_DURATION_MIN))


def _duration_for(m: Match) -> timedelta:
    return timedelta(minutes=duration_minutes(m))


# Google Calendar event colour IDs. Scheme: must-watch favorites pop in red;
# the two sports get their own colours so the calendar reads at a glance.
COLOR_FAVORITE = "11"   # Tomato  — a favorite player or England (must-watch)
COLOR_TENNIS = "10"     # Basil   — other tennis
COLOR_FOOTBALL = "9"    # Blueberry — other football


def _event_color(m: Match) -> str:
    if (m.score_breakdown or {}).get("favorite", 0.0) > 0:
        return COLOR_FAVORITE
    return COLOR_FOOTBALL if m.tour == "football" else COLOR_TENNIS


def match_to_event(m: Match) -> dict:
    """Legacy per-match event format — kept for backwards-compat but no
    longer used by main.py, which now groups matches into per-broadcaster
    daily sessions via build_session_events()."""
    if m.event_clip_start_utc and m.event_clip_end_utc:
        start = m.event_clip_start_utc.astimezone(LONDON)
        end = _clip_end(start, m.event_clip_end_utc.astimezone(LONDON), m)
    else:
        start = m.start_utc.astimezone(LONDON)
        end = _clip_end(start, start + _duration_for(m), m)
    return {
        "id": calendar_event_id(m),
        "summary": event_title(m),
        "location": uk_broadcaster_for_match(m),
        "description": event_description(m),
        "colorId": _event_color(m),
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/London"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/London"},
        "source": {
            "title": "Sofascore live",
            "url": f"https://www.sofascore.com/event/{m.sofa_id}",
        },
    }


# --- Grouped (session) calendar events --------------------------------------
#
# User said: "your time is usually off anyway, so how about like roughtly the
# time of the matches, and ten say just turn on TNT Sports at that time and
# open the first match i see". So we group all the day's pushable matches by
# broadcaster, emit ONE event per (date, broadcaster) spanning the session,
# and put the matches in the description as roughly-timed hints.

def _round_down_to_quarter(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _round_up_to_quarter(dt):
    rd = _round_down_to_quarter(dt)
    return rd if rd == dt else rd + timedelta(minutes=15)


def build_session_events(matches: list[Match]) -> list[dict]:
    """Group matches by (London date, UK broadcaster) → one calendar event
    per group. Each event:
      - title:   "Tennis on <broadcaster>"
      - location: <broadcaster>
      - start:   earliest match start, rounded down to nearest 15 min
      - end:     latest match end, rounded up + bedtime-capped
      - description: bullet list of matches with their rough times
    """
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for m in matches:
        date_key = m.start_utc.astimezone(LONDON).date().isoformat()
        bc = uk_broadcaster(m.tournament_slug)
        groups[(date_key, bc)].append(m)

    out: list[dict] = []
    for (date_key, bc), group in sorted(groups.items()):
        group.sort(key=lambda x: x.start_utc)
        first_start_local = group[0].start_utc.astimezone(LONDON)
        first_start = _round_down_to_quarter(first_start_local)
        latest_end_local = max(
            (m.event_clip_end_utc or (m.start_utc + _duration_for(m))).astimezone(LONDON)
            for m in group
        )
        latest_end = _round_up_to_quarter(_clip_to_bedtime(first_start, latest_end_local))

        lines = [f"Open {bc} and watch the first match you see. Today:"]
        for m in group:
            t = m.start_utc.astimezone(LONDON).strftime("%H:%M")
            rank_a = f"#{m.player_a.ranking}" if m.player_a.ranking else "NR"
            rank_b = f"#{m.player_b.ranking}" if m.player_b.ranking else "NR"
            lines.append(
                f"  ~{t}  {m.player_a.short_name} ({rank_a}) vs "
                f"{m.player_b.short_name} ({rank_b})  — "
                f"{_tournament_display(m.tournament_slug)} {m.round_short}"
            )

        eid_seed = f"st-session-{date_key}-{bc.lower().replace(' ', '-')}"
        event_id = "st" + hashlib.sha1(eid_seed.encode("utf-8")).hexdigest()

        out.append({
            "id": event_id,
            "summary": f"Tennis on {bc}",
            "location": bc,
            "description": "\n".join(lines),
            "start": {"dateTime": first_start.isoformat(), "timeZone": "Europe/London"},
            "end": {"dateTime": latest_end.isoformat(), "timeZone": "Europe/London"},
        })
    return out


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


# --- OAuth user credentials (installed/desktop-app flow) --------------------
#
# The original design used a Google service account with the target calendar
# shared to it. When no service account is configured we fall back to OAuth
# *user* credentials — a desktop-app client JSON plus a saved token with a
# refresh_token (the exact shape produced by google-auth-oauthlib and by the
# Node googleapis library, e.g. the foodcycle-slot-scout creds). We refresh
# the access token on the fly; no interactive consent happens here.

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _build_oauth_service(client_json: str, token_json: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    client = json.loads(client_json)
    client = client.get("installed") or client.get("web") or client
    token = json.loads(token_json)

    creds = Credentials(
        token=token.get("access_token") or token.get("token"),
        refresh_token=token.get("refresh_token"),
        token_uri=token.get("token_uri") or GOOGLE_TOKEN_URI,
        client_id=client.get("client_id") or token.get("client_id"),
        client_secret=client.get("client_secret") or token.get("client_secret"),
        scopes=token.get("scopes") or (token.get("scope") or "").split() or SCOPES,
    )
    # The stored access token is usually expired; refresh it if we can.
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _read_file_or_inline(value: str) -> str:
    if value.lstrip().startswith("{"):
        return value
    with open(value, "r", encoding="utf-8") as f:
        return f.read()


def build_calendar_service():
    """Build an authed Google Calendar service from env-configured credentials.

    Preference order:
      1. Service account  — GOOGLE_SERVICE_ACCOUNT_JSON (path or inline JSON).
      2. OAuth user creds — GOOGLE_OAUTH_CLIENT_JSON + GOOGLE_OAUTH_TOKEN_JSON
         (paths or inline JSON), e.g. a reused desktop-app client + token with
         a refresh_token.
    """
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa or os.path.exists(os.path.join(os.getcwd(), "service-account.json")):
        try:
            return _build_service(_load_service_account_json())
        except (FileNotFoundError, ValueError, KeyError) as e:
            log.warning("service-account auth unavailable (%s); trying OAuth user creds", e)

    client_env = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON")
    token_env = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON")
    if client_env and token_env:
        return _build_oauth_service(_read_file_or_inline(client_env), _read_file_or_inline(token_env))

    # Last resort: attempt service-account path (raises a clear error).
    return _build_service(_load_service_account_json())


def prune_orphans(service, calendar_id: str, keep_event_ids: set[str],
                  time_min, time_max, dry_run: bool = False) -> int:
    """Delete previously-pushed events in [time_min, time_max] that are no
    longer in the current selection. Period.

    Earlier versions tried to protect "ongoing" events (start past, end
    future) from being yanked mid-watch, but that protection is redundant
    now that scrape.py includes status='inprogress' matches in the
    selection — any genuinely live match the user is watching IS in
    keep_event_ids. The protection was just masking a real bug: when
    the user raises the threshold or adds a blackout rule, stale events
    from earlier runs (Auger-Aliassime at score 8 once the threshold
    moves to 9) should be deleted even if their block hasn't elapsed.

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
    """Upsert each match as its own calendar event with player-named title.

    The build_session_events() helper is kept in this module for the
    grouped-by-broadcaster mode but isn't called here per user revert.

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
