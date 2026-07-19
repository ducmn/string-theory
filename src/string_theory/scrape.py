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

import functools
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from curl_cffi import requests as curl_requests

from .models import Match, Player

log = logging.getLogger(__name__)

DEFAULT_API = "https://api.sofascore.com/api/v1"
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

# Sofascore retired the flat `/sport/{sport}/scheduled-events/{date}` endpoint
# (it now 404s for every date). The per-category variant
# `/category/{id}/scheduled-events/{date}` still works, so we fetch by
# category instead. These are the tennis category IDs that carry main-tour
# singles — Grand Slams show up under the ATP/WTA categories too (e.g.
# Wimbledon has category.slug "atp"/"wta", tennisPoints 2000).
TENNIS_CATEGORIES = {3: "atp", 6: "wta"}

# Football category IDs to scan. `/category/{id}/scheduled-events/{date}`
# returns every competition tagged under that category; we then filter down
# to FOOTBALL_ALLOWLIST by uniqueTournament slug + round. World = FIFA World
# Cup / international; Europe = UEFA club comps + Euros; the domestic ones
# cover the national cup finals in the allowlist.
FOOTBALL_CATEGORIES = {
    1468: "World",
    1465: "Europe",
    1469: "North & Central America",
    1470: "South America",
    1: "England",
    32: "Spain",
    31: "Italy",
    30: "Germany",
}


_TIER_BY_POINTS = {
    2000: "GS",
    1000: "MASTERS",   # resolved to M1000/W1000 once we know the tour
    500: "T500",
    250: "T250",
}

_ROUND_SHORT = {
    "Final": "F",
    # Sofascore returns plural round names ("Semifinals"); keep the singular
    # forms too in case the schema differs across endpoints/sports.
    "Semifinals": "SF",
    "Semifinal": "SF",
    "Quarterfinals": "QF",
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
    """Raw tennis events for a given calendar date (YYYY-MM-DD).

    Fetches each main-tour category (ATP, WTA) and concatenates. The old
    flat `/sport/tennis/scheduled-events/{date}` endpoint is dead (404).
    """
    events: list[dict] = []
    for cid in TENNIS_CATEGORIES:
        try:
            events.extend(
                _get_json_path(f"/category/{cid}/scheduled-events/{date}").get("events", [])
            )
        except RuntimeError as e:
            # A single failing category shouldn't sink the whole date.
            log.warning("category %s scheduled-events failed for %s: %s", cid, date, e)
    return events


def _round_short(name: str) -> str:
    return _ROUND_SHORT.get(name, name.replace(" ", ""))


def _strip_gender(name: str) -> str:
    """Sofascore names Grand Slam draws "Wimbledon, Men" / "Wimbledon, Women".
    The user doesn't want the gender qualifier, so drop a trailing ", Men" /
    ", Women" (and the bare " Men"/" Women" variant)."""
    for suffix in (", Men", ", Women", " Men", " Women"):
        if name.endswith(suffix):
            return name[: -len(suffix)].rstrip(", ")
    return name


@functools.lru_cache(maxsize=256)
def _region_for(lat: float, lon: float) -> str | None:
    """Reverse-geocode coordinates to a state/province (e.g. "New Jersey",
    "England") via BigDataCloud's free, keyless endpoint. Best-effort: any
    failure returns None so the caller falls back to venue + country. Cached
    so repeat venues in one run don't re-hit the API."""
    url = (f"https://api.bigdatacloud.net/data/reverse-geocode-client"
           f"?latitude={lat}&longitude={lon}&localityLanguage=en")
    try:
        r = curl_requests.get(url, headers={"User-Agent": "string-theory/1.0"}, timeout=10)
        if r.status_code != 200:
            return None
        region = (r.json().get("principalSubdivision") or "").strip()
        return region or None
    except Exception as e:  # network/JSON — never fatal
        log.warning("reverse-geocode failed for %s,%s: %s", lat, lon, e)
        return None


def fetch_event_venue(sofa_id: int) -> str | None:
    """Best-effort venue + state/region + country for a single event, e.g.
    "MetLife Stadium, New Jersey, USA" or "Centre Court, England, United
    Kingdom".

    Sofascore only carries the immediate city (e.g. "East Rutherford" for
    MetLife Stadium) — often an obscure suburb — and no state/region field.
    So we skip the city and reverse-geocode the venue coordinates to the
    state/province, which is the recognisable level the user wants. Falls
    back to venue + country if the coordinates or geocode are unavailable.

    Only the per-event detail endpoint carries `venue`; the scheduled-events
    list does not. Called just for the handful of finally-selected matches so
    we don't hit this endpoint for every candidate. Returns None on any
    failure — venue is a nice-to-have, never fatal."""
    try:
        data = _get_json_path(f"/event/{sofa_id}")
    except RuntimeError:
        return None
    venue = ((data.get("event") or {}).get("venue")) or {}
    name = venue.get("name")
    country = ((venue.get("country") or (venue.get("city") or {}).get("country")) or {}).get("name")
    coords = venue.get("venueCoordinates") or {}
    lat, lon = coords.get("latitude"), coords.get("longitude")
    region = _region_for(lat, lon) if lat is not None and lon is not None else None
    # Drop a region that just repeats the country (e.g. small nations).
    if region and country and region.lower() == country.lower():
        region = None
    return ", ".join(p for p in (name, region, country) if p) or None


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
    if "singles" not in cats:
        return False
    # Sofascore occasionally mis-tags a doubles match as "singles" (e.g. a
    # women's doubles pair). Doubles team names are always "Surname X /
    # Surname Y" — reject anything with a slash on either side.
    for side in ("homeTeam", "awayTeam"):
        if "/" in ((event.get(side) or {}).get("name") or ""):
            return False
    return True


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
            tournament_name=_strip_gender(ut.get("name") or ""),
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


# Football competitions whose knockout matches should auto-land in the
# calendar. Keys are uniqueTournament.slug from Sofa; values are round-name
# substrings that qualify. League football (Premier League, La Liga, etc.)
# is intentionally excluded — too many matches, too much noise.
FOOTBALL_ALLOWLIST: dict[str, list[str]] = {
    "uefa-champions-league": ["Round of 16", "Quarterfinal", "Semifinal", "Final"],
    "uefa-europa-league": ["Quarterfinal", "Semifinal", "Final"],
    "uefa-europa-conference-league": ["Semifinal", "Final"],
    # Sofascore's slug for the FIFA World Cup is "world-championship"
    # (uniqueTournament id 16). Round names are plural ("Quarterfinals",
    # "Semifinals") — the substring match below handles that.
    "world-championship": ["Round of 16", "Quarterfinal", "Semifinal", "Final"],
    "european-championship": ["Quarterfinal", "Semifinal", "Final"],
    "copa-america": ["Quarterfinal", "Semifinal", "Final"],
    "concacaf-champions-cup": ["Final"],
    "fa-cup": ["Final"],
    "copa-del-rey": ["Final"],
    "coppa-italia": ["Final"],
    "dfb-pokal": ["Final"],
}

# Competitions contested by national teams (as opposed to clubs). For these,
# the user only cares about matches involving one of FOOTBALL_NATIONS below —
# club competitions are unaffected since they have no national side playing.
FOOTBALL_NATIONAL_TEAM_SLUGS = {
    "world-championship",
    "european-championship",
    "copa-america",
}

# National sides the user follows. A national-team match is only kept if one of
# the two teams is in this set (matched on the Sofascore team name) — EXCEPT
# for the rounds in FOOTBALL_NATION_EXEMPT_ROUNDS below.
FOOTBALL_NATIONS = {"England", "France"}

# Marquee rounds that are must-watch regardless of nation: the user wants the
# World Cup / Euros / Copa final even when England and France aren't in it.
# ("Final" is capitalised and is NOT a substring of "Semifinal"/"Quarterfinal",
# so this matches the final only.)
FOOTBALL_NATION_EXEMPT_ROUNDS = ("Final",)


def _team_to_football_team(team: dict) -> Player:
    return Player(
        sofa_id=team.get("id") or 0,
        full_name=team.get("name") or "",
        short_name=team.get("shortName") or team.get("name") or "",
        country_code=(team.get("country") or {}).get("alpha3") or "",
        slug=team.get("slug") or "",
        ranking=None,
    )


def _round_to_short_football(name: str) -> str:
    return _ROUND_SHORT.get(name, name)


def fetch_upcoming_football_matches(days_ahead: int = 5) -> list[Match]:
    """Pull knockout-round football matches across the FOOTBALL_ALLOWLIST
    competitions for the next `days_ahead` days."""
    today = datetime.now(timezone.utc).date()
    matches: list[Match] = []
    seen_ids: set[int] = set()
    for d in range(days_ahead + 1):
        date_str = (today + timedelta(days=d)).isoformat()
        log.info("Fetching football events for %s", date_str)
        events: list[dict] = []
        for cid in FOOTBALL_CATEGORIES:
            try:
                events.extend(
                    _get_json_path(f"/category/{cid}/scheduled-events/{date_str}").get("events", [])
                )
            except RuntimeError as e:
                log.warning("football category %s failed for %s: %s", cid, date_str, e)
        for ev in events:
            if (ev.get("status") or {}).get("type") not in ("notstarted", "inprogress"):
                continue
            ts = ev.get("startTimestamp")
            if not ts:
                continue
            ut = (ev.get("tournament") or {}).get("uniqueTournament") or {}
            slug = ut.get("slug") or ""
            if slug not in FOOTBALL_ALLOWLIST:
                continue
            round_name = (ev.get("roundInfo") or {}).get("name") or ""
            if not any(r in round_name for r in FOOTBALL_ALLOWLIST[slug]):
                continue
            # National-team comps (World Cup, Euros, Copa) are restricted to the
            # nations the user follows; club comps pass through untouched. The
            # final is exempt — it's must-watch whoever's in it.
            if (slug in FOOTBALL_NATIONAL_TEAM_SLUGS
                    and not any(r in round_name for r in FOOTBALL_NATION_EXEMPT_ROUNDS)):
                names = {
                    (ev.get("homeTeam") or {}).get("name"),
                    (ev.get("awayTeam") or {}).get("name"),
                }
                if not (names & FOOTBALL_NATIONS):
                    continue
            sofa_id = ev.get("id")
            if sofa_id in seen_ids:
                continue
            seen_ids.add(sofa_id)
            # Football seasons are often "25/26" or "2025/2026" — pick the
            # end-year as the canonical season year.
            season_raw = str(ev.get("season", {}).get("year") or "")
            try:
                year = int(season_raw.split("/")[-1]) if season_raw else datetime.utcnow().year
                if year < 100:
                    year += 2000
            except ValueError:
                year = datetime.utcnow().year
            matches.append(Match(
                sofa_id=sofa_id,
                tour="football",
                tournament_slug=slug,
                tournament_name=ut.get("name") or "",
                tournament_tier="FOOTBALL",
                surface="",
                year=year,
                round_name=round_name,
                round_short=_round_to_short_football(round_name),
                start_utc=datetime.fromtimestamp(ts, tz=timezone.utc),
                player_a=_team_to_football_team(ev["homeTeam"]),
                player_b=_team_to_football_team(ev["awayTeam"]),
            ))
    matches.sort(key=lambda m: m.start_utc)
    log.info("Loaded %d football matches", len(matches))
    return matches


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
