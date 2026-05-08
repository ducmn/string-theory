"""Sofascore tennis client.

Sofascore is a third-party livescore site whose unauthenticated JSON API is
widely used by community projects. We pick it as the v1 data source because:

- It covers ATP and WTA in a single schema (atptour.com is bot-blocked, and
  scraping two tour sites separately is more code than this is worth).
- No API key, no rate-limit auth, single host.
- Clean fields we need: scheduled start as UNIX timestamp, tournament tier
  via `tennisPoints`, round via `roundInfo.name`, singles/doubles flag via
  `eventFilters.category`, surface via `groundType`.

We use `curl_cffi` to send a Chrome-style TLS fingerprint. Sofascore is
behind Cloudflare, which 403's plain Python `urllib` (and most HTTP clients)
based on the JA3 fingerprint, not just headers — particularly from cloud
egress IPs like GitHub Actions. `curl_cffi` matches a real browser's
fingerprint at the TLS layer and slips through reliably.

If they ever break the schema, this module is the only thing that needs to
change.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from curl_cffi import requests as curl_requests

from .models import Match, Player

log = logging.getLogger(__name__)

DEFAULT_API = "https://www.sofascore.com/api/v1"
IMPERSONATE = "chrome"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
}


def _api_base() -> str:
    """Sofascore API base URL.

    From a residential IP, hit Sofascore directly. From a cloud-egress IP
    (GitHub Actions, AWS, GCP) Sofascore returns 403 via Cloudflare regardless
    of TLS fingerprint, so set SOFASCORE_PROXY_BASE to your deployed
    Cloudflare Worker URL (see worker/sofascore-proxy.js).
    """
    return (os.environ.get("SOFASCORE_PROXY_BASE") or DEFAULT_API).rstrip("/")

ATP_RANKING_TYPE = 5
WTA_RANKING_TYPE = 6


_TIER_BY_POINTS = {
    2000: "GS",
    1000: "MASTERS",   # resolved to M1000/W1000 once we know the tour
    500: "T500",
    250: "T250",
}

_ROUND_SHORT = {
    "Final": "F",
    "Semifinal": "SF",
    "Quarterfinal": "QF",
    "Round of 16": "R16",
    "Round of 32": "R32",
    "Round of 64": "R64",
    "Round of 128": "R128",
}


def _get_json_path(path: str, retries: int = 3, sleep: float = 1.5) -> dict:
    """Fetch a Sofascore API path with browser-fingerprint TLS via curl_cffi."""
    url = f"{_api_base()}{path}"
    last_err: Exception = RuntimeError("not attempted")
    for attempt in range(retries):
        try:
            r = curl_requests.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=20)
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code}")
            log.warning("GET %s -> %s, retrying", url, r.status_code)
        except Exception as e:  # curl_cffi raises its own exception types
            last_err = e
            log.warning("GET %s failed (%s), retrying", url, e)
        time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"GET {path} failed after {retries} attempts: {last_err}")


def fetch_rankings() -> dict[int, int]:
    """Return {sofa_team_id -> ranking} for top-ranked ATP+WTA singles players."""
    out: dict[int, int] = {}
    for tour_type in (ATP_RANKING_TYPE, WTA_RANKING_TYPE):
        data = _get_json_path(f"/rankings/type/{tour_type}")
        for row in data.get("rankings", []):
            team = row.get("team") or {}
            tid = team.get("id")
            rank = team.get("ranking") or row.get("ranking")
            if tid and rank:
                out[tid] = rank
    log.info("Loaded %d player rankings", len(out))
    return out


def fetch_scheduled_events(date: str) -> list[dict]:
    """Raw events for a given calendar date (YYYY-MM-DD)."""
    return _get_json_path(f"/sport/tennis/scheduled-events/{date}").get("events", [])


def _round_short(name: str) -> str:
    return _ROUND_SHORT.get(name, name.replace(" ", ""))


def _normalize_surface(s: str) -> str:
    s = (s or "").strip().lower()
    for prefix in ("red ", "green "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s or "unknown"


def _tier_label(tour: str, tennis_points: int | None) -> str:
    raw = _TIER_BY_POINTS.get(tennis_points or 0, "OTHER")
    if raw == "MASTERS":
        return "M1000" if tour == "atp" else "W1000"
    if raw == "T500":
        return "ATP500" if tour == "atp" else "WTA500"
    if raw == "T250":
        return "ATP250" if tour == "atp" else "WTA250"
    return raw


def _team_to_player(team: dict, rankings: dict[int, int]) -> Player:
    return Player(
        sofa_id=team.get("id") or 0,
        full_name=team.get("name") or "",
        short_name=team.get("shortName") or team.get("name") or "",
        country_code=(team.get("country") or {}).get("alpha3") or "",
        slug=team.get("slug") or "",
        ranking=rankings.get(team.get("id") or 0),
    )


def _is_singles(event: dict) -> bool:
    cats = (event.get("eventFilters") or {}).get("category") or []
    return "singles" in cats


def _is_main_tour(event: dict) -> bool:
    cat = (
        ((event.get("tournament") or {}).get("uniqueTournament") or {}).get("category") or {}
    ).get("slug")
    return cat in {"atp", "wta"}


def normalize_events(events: Iterable[dict], rankings: dict[int, int]) -> list[Match]:
    out: list[Match] = []
    for ev in events:
        if not _is_singles(ev):
            continue
        if not _is_main_tour(ev):
            continue
        # Include both upcoming AND in-progress matches: an ongoing match
        # should stay in the user's calendar (with end time covered by our
        # generous duration block) until it finishes. We never re-add a
        # finished match.
        if (ev.get("status") or {}).get("type") not in ("notstarted", "inprogress"):
            continue
        ts = ev.get("startTimestamp")
        if not ts:
            continue
        ut = ev["tournament"]["uniqueTournament"]
        tour = ut["category"]["slug"]
        round_name = (ev.get("roundInfo") or {}).get("name") or "?"
        match = Match(
            sofa_id=ev["id"],
            tour=tour,
            tournament_slug=ut.get("slug") or "",
            tournament_name=ut.get("name") or "",
            tournament_tier=_tier_label(tour, ut.get("tennisPoints")),
            surface=_normalize_surface(ev.get("groundType") or ut.get("groundType") or ""),
            year=int(ev.get("season", {}).get("year") or datetime.utcnow().year),
            round_name=round_name,
            round_short=_round_short(round_name),
            start_utc=datetime.fromtimestamp(ts, tz=timezone.utc),
            player_a=_team_to_player(ev["homeTeam"], rankings),
            player_b=_team_to_player(ev["awayTeam"], rankings),
        )
        out.append(match)
    return out


def fetch_upcoming_matches(days_ahead: int = 2) -> list[Match]:
    """Pull main-tour singles matches scheduled within the next `days_ahead` days."""
    rankings = fetch_rankings()
    today = datetime.now(timezone.utc).date()
    matches: list[Match] = []
    seen_ids: set[int] = set()
    for d in range(days_ahead + 1):
        date_str = (today + timedelta(days=d)).isoformat()
        log.info("Fetching events for %s", date_str)
        events = fetch_scheduled_events(date_str)
        for m in normalize_events(events, rankings):
            if m.sofa_id in seen_ids:
                continue
            seen_ids.add(m.sofa_id)
            matches.append(m)
    matches.sort(key=lambda m: m.start_utc)
    return matches
