"""Sofascore tennis client.

Sofascore is a third-party livescore site whose unauthenticated JSON API is
widely used by community projects. We pick it as the v1 data source because:

- It covers ATP and WTA in a single schema (atptour.com is bot-blocked, and
  scraping two tour sites separately is more code than this is worth).
- No API key, no rate-limit auth, single host.
- Clean fields we need: scheduled start as UNIX timestamp, tournament tier
  via `tennisPoints`, round via `roundInfo.name`, singles/doubles flag via
  `eventFilters.category`, surface via `groundType`.

If they ever break the schema, this module is the only thing that needs to
change.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import Match, Player

log = logging.getLogger(__name__)

# Sofascore sits behind Cloudflare, which blanket-403s a lot of cloud-egress IPs
# (GitHub Actions, AWS, GCP). We try direct hosts first; if all are blocked we
# fall through to a public CORS proxy. The proxy is best-effort — if it ever
# goes away, set SOFASCORE_PROXY_BASE in the environment to override.
DIRECT_HOSTS = [
    "https://www.sofascore.com/api/v1",
    "https://api.sofascore.com/api/v1",
]
DEFAULT_PROXY = "https://api.allorigins.win/raw?url="
API_HOSTS = DIRECT_HOSTS  # kept for backwards compat with any existing imports
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "DNT": "1",
}

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


def _decode_response(resp) -> bytes:
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        import gzip
        return gzip.decompress(raw)
    if enc == "deflate":
        import zlib
        return zlib.decompress(raw)
    if enc == "br":
        try:
            import brotli  # type: ignore
        except ImportError:
            log.warning("brotli not installed; got br-compressed response")
            return raw
        return brotli.decompress(raw)
    return raw


def _try_get(url: str, retries: int, sleep: float, headers: dict) -> dict | Exception:
    last_err: Exception = RuntimeError("not attempted")
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(_decode_response(r))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log.warning("GET %s failed (%s), retrying", url, e)
            time.sleep(sleep * (attempt + 1))
    return last_err


def _get_json_path(path: str, retries: int = 3, sleep: float = 1.5) -> dict:
    """Try direct hosts; if all fail with 403/blocked, fall through to a CORS proxy.

    GitHub Actions egress IPs are blanket-403'd by Cloudflare on the direct
    Sofascore hosts. The public proxy (https://api.allorigins.win/raw) gives
    us a fallback path. Set SOFASCORE_PROXY_BASE to override the proxy URL.
    """
    last_err: Exception = RuntimeError("not attempted")
    for host in DIRECT_HOSTS:
        result = _try_get(f"{host}{path}", retries=retries, sleep=sleep, headers=BROWSER_HEADERS)
        if not isinstance(result, Exception):
            return result
        last_err = result
        log.warning("Host %s exhausted, trying next", host)

    proxy_base = os.environ.get("SOFASCORE_PROXY_BASE", DEFAULT_PROXY)
    if proxy_base:
        target = urllib.parse.quote(f"{DIRECT_HOSTS[0]}{path}", safe="")
        proxy_url = f"{proxy_base}{target}"
        log.warning("Direct hosts blocked, trying proxy %s", proxy_base)
        # Most public proxies don't like Origin/Referer mirroring our app.
        proxy_headers = {"User-Agent": UA, "Accept": "application/json,*/*"}
        result = _try_get(proxy_url, retries=retries, sleep=sleep, headers=proxy_headers)
        if not isinstance(result, Exception):
            return result
        last_err = result

    raise RuntimeError(f"GET {path} failed across all hosts and proxy: {last_err}")


def _get_json(url_or_path: str, retries: int = 3, sleep: float = 1.5) -> dict:
    """Backwards-compatible: accepts either a path (preferred) or a full URL."""
    if url_or_path.startswith("http"):
        last_err: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(url_or_path, headers=BROWSER_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(_decode_response(r))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                last_err = e
                log.warning("GET %s failed (%s), retrying", url_or_path, e)
                time.sleep(sleep * (attempt + 1))
        raise RuntimeError(f"GET {url_or_path} failed after {retries} attempts: {last_err}")
    return _get_json_path(url_or_path, retries=retries, sleep=sleep)


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
        if (ev.get("status") or {}).get("type") != "notstarted":
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
