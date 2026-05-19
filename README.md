# string-theory

An ATP/WTA tennis match curator. Every 3 hours a GitHub Actions cron pulls
the next 2–3 days of upcoming main-tour singles matches, scores each one for
watch-worthiness, filters to the slots an evening UK viewer would actually
watch (07:00–01:00 Europe/London), de-overlaps competing matches (highest
score wins), drops anything that conflicts with the user's personal/work
calendars, and upserts the survivors as events into a dedicated Google
Calendar. Anything pushed by an earlier run that no longer qualifies gets
pruned.

State lives in the calendar via deterministic event IDs — re-runs update
existing events instead of duplicating, so it's safe to fire the cron more
than once a day, or kick a manual run from the Actions tab.

```
sofascore  ─►  scrape.py  ─►  Match  ─►  score.py  ─►  filter (window+threshold)
                                                            │
                                                            ▼
                                       calendar_push.py  ─►  Google Calendar
```

## Why Sofascore as the data source

The original spec preferred official tour APIs. The reality:

- **api.wtatennis.com** is a clean Pulselive-backed JSON API. Works fine.
- **atptour.com** is locked behind Akamai bot protection — every direct
  fetch returns HTTP 403, including via WebFetch.

Maintaining two scrapers (one for WTA, a tortuous one for ATP) is more code
than this project is worth. **api.sofascore.com** ships a single schema
covering both tours, no API key, with all the fields we need
(`startTimestamp`, `tennisPoints` for tier, `roundInfo`, `eventFilters` for
singles/doubles, team IDs that join cleanly to the rankings endpoint).

If Sofascore breaks the schema, [`src/string_theory/scrape.py`](src/string_theory/scrape.py) is the only file that
needs to change.

## Scoring

Hardcoded weights, easy to tune in [`src/string_theory/score.py`](src/string_theory/score.py):

```
score = tier + round + ranking + favorite + headliner

tier_weight:    GS=5  M1000/W1000=4  ATP500/WTA500=3  ATP250/WTA250=1
round_weight:   F=5  SF=4  QF=3  R16=2  R32=1  R64=0.5  earlier=0
ranking_score:  both top10=5  top10 vs top20=3  both top50=2  both top100=1  else 0
favorite_bonus: +2 if Learner Tien is in the match (sole personal pick)
headliner_bonus: +2 if either player is currently top-5 (catches Djokovic
                vs Prižmić in an M1000 R64 — would otherwise score 5.5 and
                fall under threshold)
```

Push if `score >= 6.0`.

## Calendar event format

- **Title**: `Sinner vs Alcaraz, Madrid R16 (clay)`
- **Description**: full tournament name, both players with rankings + nationality, score breakdown, UK broadcaster, live-score URL
- **Time**: scheduled start in Europe/London. Duration scales with round (R64 = 90m, QF/SF/F = 3h)
- **Event ID**: `st<sha1(legible-key)>` — deterministic, idempotent, fits Google Calendar's `[a-v0-9]{5,1024}` constraint
- **Where to watch (UK)**: hand-curated mapping in [`broadcaster.py`](src/string_theory/broadcaster.py).
  Wimbledon → BBC iPlayer, Roland Garros / Australian Open → TNT Sports on HBO Max, everything else → NowTV.

## Run locally

```bash
pip install -e .[dev]

# Show what would be pushed without writing to the calendar
GOOGLE_SERVICE_ACCOUNT_JSON=./service-account.json \
TARGET_CALENDAR_ID=<your-calendar-id>@group.calendar.google.com \
  python -m string_theory.main --dry-run

# Push for real
GOOGLE_SERVICE_ACCOUNT_JSON=./service-account.json \
TARGET_CALENDAR_ID=<your-calendar-id>@group.calendar.google.com \
  python -m string_theory.main

# Show top 30 candidates with scores (no filter, no push) — for tuning
PYTHONPATH=src python -m string_theory.main --dry-run --all
```

`GOOGLE_SERVICE_ACCOUNT_JSON` accepts either a path to the JSON file or the
JSON content inline (useful for GitHub Actions secrets).

## Tests

```bash
pytest -q
```

Covers the scoring function and a regression test for the
Djokovic-vs-Prižmić acceptance case.

## GCP setup (one-off)

1. Create a new Google Cloud project (or pick an existing one).
2. Enable the **Google Calendar API**.
3. Create a **service account**. Download its JSON key — that's the file
   referenced by `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. Create a dedicated Google Calendar (e.g. "Tennis").
5. **Share the calendar with the service account's email** (looks like
   `<name>@<project>.iam.gserviceaccount.com`) and grant
   "Make changes to events". This is the step everyone forgets.
6. Find the calendar ID under Calendar Settings → Integrate calendar →
   Calendar ID. Save it as `TARGET_CALENDAR_ID`.

## Full-automation tradeoffs

There's an ugly truth in the data layer: Sofascore (the upstream API) sits behind
Cloudflare and 403s **every GitHub Actions egress IP** regardless of TLS/headers.
Bypassing it requires a proxy that runs on infrastructure Cloudflare doesn't
blanket-block — and on free-tier accounts I've found:

- **Cloudflare Workers** — natural fit (CF won't block CF), but new accounts'
  `workers.dev` SSL provisioning sometimes stalls indefinitely.
- **Deno Deploy v2** — fast deploy, but unverified-org accounts hit a 403 bot
  challenge on direct API requests, and per-app subdomains aren't covered by
  the wildcard cert.

Until one of those gets unstuck, **the cron runs locally on macOS via launchd**.
That works perfectly while the Mac is awake but pauses when it sleeps.

For *truly* always-on automation you have three options, in order of effort:

1. **Run on AC + a `caffeinate` daemon** — load the included
   [`launchd/com.ducmn.string-theory.caffeinate.plist`](launchd/com.ducmn.string-theory.caffeinate.plist) into
   `~/Library/LaunchAgents/` then `launchctl load` it. This keeps a
   `caffeinate -s -i` process alive in your user session so the Mac stays
   awake on AC even when the lid is closed (battery still sleeps — by
   design, so we don't drain it). Combined with the cron plist, the Mac
   becomes a reliable always-on runner as long as it's plugged in.

2. **Deploy the included Cloudflare Worker proxy** — when CF's SSL provisioning
   does eventually catch up, set `SOFASCORE_PROXY_BASE` to the Worker URL and
   uncomment the cron line in `.github/workflows/daily.yml`.

3. **Pay $5/mo for a small VPS** (Fly.io, Hetzner, DigitalOcean droplet) and
   run `python -m string_theory.main` from a systemd timer there. Zero local
   dependency, fully always-on.

## Run locally on macOS via launchd (alternative to GitHub Actions)

If you don't want to deploy the Cloudflare Worker (next section), running the
cron from your Mac is the simplest path — your residential IP can hit
Sofascore directly. A sample plist is included as
[`launchd/com.ducmn.string-theory.plist`](launchd/com.ducmn.string-theory.plist) in this repo. Copy it into
`~/Library/LaunchAgents/`, edit the `EnvironmentVariables` dict to point at
your service-account JSON and calendar ID, then:

```bash
launchctl load ~/Library/LaunchAgents/com.ducmn.string-theory.plist
launchctl start com.ducmn.string-theory   # one-shot to verify
tail -f /tmp/string-theory.log
```

Logs go to `/tmp/string-theory.log`. The plist runs every 3 hours, on the
hour. Disable with `launchctl unload ~/Library/LaunchAgents/com.ducmn.string-theory.plist`.

## The Cloudflare Worker proxy (one-time, ~5 min)

Sofascore is on Cloudflare, which 403s **all** GitHub Actions egress IPs
regardless of TLS fingerprint or headers. Cloudflare's own egress isn't
blocked (because it *is* Cloudflare). [`worker/sofascore-proxy.js`](worker/sofascore-proxy.js) is a ~30-line
Worker that forwards requests; it's free up to 100k requests/day.

```bash
cd worker
npx wrangler login           # one-time browser auth
npx wrangler deploy          # publishes to <name>.<your-handle>.workers.dev
```

Take the URL it prints, append `/api/v1`, and store it as a GitHub Actions
secret named `SOFASCORE_PROXY_BASE`:

```bash
gh secret set SOFASCORE_PROXY_BASE \
  --body "https://sofascore-proxy.<your-handle>.workers.dev/api/v1"
```

(Locally on a residential connection the direct host works fine; setting
this env var is only needed in cloud envs like GitHub Actions.)

## GitHub Actions setup

Two required repository secrets:

- `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the entire JSON file contents
- `TARGET_CALENDAR_ID` — the calendar ID from step 6 above

Optional:

- `BUSY_CALENDAR_IDS` — comma-separated list of **Google** calendar IDs to
  consult for conflicts. For each candidate match we query Google's freeBusy
  API; if any of those calendars shows you busy during the match window, the
  match is skipped instead of pushed. The service account must be granted
  "See all event details" on each.
- `BUSY_ICS_URLS` — comma-separated list of public ICS feed URLs to ingest
  as additional busy sources. **Use this for Office365/Exchange calendars**
  (or anything else not on Google) — Google's freeBusy can't reach them, but
  Outlook can publish a calendar as an ICS URL:

  > Outlook on the web → Settings → Calendar → Shared calendars →
  > Publish a calendar → pick the calendar → permission "Can view all
  > details" (or at minimum "Can view when I'm busy") → publish → copy
  > the **ICS** link (not the HTML one).

  Note: corporate Workspace admins often disable external publishing —
  if your org does, you'll need IT to enable it for your calendar.

The workflow at [`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs every 3 hours and exposes a
manual `workflow_dispatch` trigger with a dry-run toggle. A separate `test`
job runs `pytest` on every push.

## Out of scope (v1)

- Conflict-checking against my main calendar — push to a separate "Tennis"
  calendar instead, my eyes do the resolution.
- LLM-based scoring — v2.
- In-match push notifications, live score streaming.
- Doubles, mixed doubles, Challengers.
