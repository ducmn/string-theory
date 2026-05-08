from dataclasses import replace
from datetime import datetime, timedelta, timezone

from string_theory.conflicts import filter_against_busy, pick_non_overlapping
from string_theory.models import Match, Player


def make_match(*, sofa_id: int, start: datetime, round_short: str = "R32", score: float = 6.0) -> Match:
    p = Player(sofa_id=sofa_id * 10, full_name=f"P{sofa_id}", short_name=f"P{sofa_id}",
               country_code="USA", slug=f"p{sofa_id}", ranking=10)
    q = Player(sofa_id=sofa_id * 10 + 1, full_name=f"Q{sofa_id}", short_name=f"Q{sofa_id}",
               country_code="USA", slug=f"q{sofa_id}", ranking=20)
    return Match(
        sofa_id=sofa_id, tour="atp", tournament_slug="rome", tournament_name="ATP Rome Masters",
        tournament_tier="M1000", surface="clay", year=2026,
        round_name="Round of 32", round_short=round_short, start_utc=start,
        player_a=p, player_b=q, score=score,
    )


def test_dedup_keeps_higher_score_when_overlap():
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    # R32 duration is 180 min, so we space the non-overlapping match well clear.
    a = make_match(sofa_id=1, start=t, round_short="R32", score=7.0)
    b = make_match(sofa_id=2, start=t + timedelta(minutes=30), score=11.0)      # overlaps, higher
    c = make_match(sofa_id=3, start=t + timedelta(hours=4), score=6.5)          # well after a/b

    kept = pick_non_overlapping([a, b, c])
    assert {m.sofa_id for m in kept} == {2, 3}
    assert [m.sofa_id for m in kept] == [2, 3]


def test_dedup_no_overlap_keeps_all():
    t = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc)
    # R32 = 180 min block; space matches 4h apart to clear.
    matches = [
        make_match(sofa_id=1, start=t),
        make_match(sofa_id=2, start=t + timedelta(hours=4)),
        make_match(sofa_id=3, start=t + timedelta(hours=8)),
    ]
    kept = pick_non_overlapping(matches)
    assert len(kept) == 3


def test_busy_filter_drops_overlapping():
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    matches = [
        make_match(sofa_id=1, start=t),                                  # busy here
        make_match(sofa_id=2, start=t + timedelta(hours=4)),             # free
    ]
    busy = [(t - timedelta(minutes=30), t + timedelta(minutes=30))]      # blocks #1
    out = filter_against_busy(matches, busy)
    assert [m.sofa_id for m in out] == [2]


def test_busy_filter_empty_passthrough():
    t = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc)
    matches = [make_match(sofa_id=1, start=t)]
    assert filter_against_busy(matches, []) == matches


def test_ics_parsing_handles_utc_and_tzid_and_folded_lines(tmp_path, monkeypatch):
    """Stdlib ICS parser should handle UTC zulu, TZID params, and folded lines."""
    from string_theory.conflicts import _parse_ics_dt, fetch_ics_busy_intervals
    from zoneinfo import ZoneInfo

    LONDON = ZoneInfo("Europe/London")

    block_utc = "DTSTART:20260509T093000Z\nDTEND:20260509T100000Z\n"
    s = _parse_ics_dt(block_utc, "DTSTART", LONDON)
    e = _parse_ics_dt(block_utc, "DTEND", LONDON)
    assert s == datetime(2026, 5, 9, 9, 30, tzinfo=timezone.utc)
    assert e == datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)

    block_tzid = "DTSTART;TZID=Europe/London:20260509T103000\nDTEND;TZID=Europe/London:20260509T120000\n"
    s = _parse_ics_dt(block_tzid, "DTSTART", LONDON)
    assert s == datetime(2026, 5, 9, 10, 30, tzinfo=LONDON)

    # End-to-end via a file:// URL — the function fetches with urllib so we
    # can point it at a temp file.
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:1@test\r\n"
        "SUMMARY:Standup with a\r\n  long folded summary line\r\n"
        "DTSTART:20260509T080000Z\r\n"
        "DTEND:20260509T083000Z\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:2@test\r\n"
        "DTSTART;TZID=Europe/London:20260509T093000\r\n"
        "DTEND;TZID=Europe/London:20260509T100000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    f = tmp_path / "calendar.ics"
    f.write_text(ics)

    intervals = fetch_ics_busy_intervals(
        [f.as_uri()],
        datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert len(intervals) == 2
    starts = sorted(i[0] for i in intervals)
    assert starts[0] == datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)


def test_ics_skips_all_day_and_multi_day_events(tmp_path):
    """All-day vacation/OOO and multi-day events shouldn't block individual matches."""
    from string_theory.conflicts import fetch_ics_busy_intervals

    ics = (
        "BEGIN:VCALENDAR\r\n"
        # All-day OOO — should be skipped
        "BEGIN:VEVENT\r\n"
        "UID:ooo@test\r\n"
        "DTSTART;VALUE=DATE:20260427\r\n"
        "DTEND;VALUE=DATE:20260509\r\n"
        "SUMMARY:Out of Office\r\n"
        "END:VEVENT\r\n"
        # Multi-day timed event (12 days) — should be skipped
        "BEGIN:VEVENT\r\n"
        "UID:vacation@test\r\n"
        "DTSTART:20260501T000000Z\r\n"
        "DTEND:20260513T000000Z\r\n"
        "END:VEVENT\r\n"
        # Normal 1h meeting — should be kept
        "BEGIN:VEVENT\r\n"
        "UID:standup@test\r\n"
        "DTSTART:20260509T100000Z\r\n"
        "DTEND:20260509T110000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    f = tmp_path / "calendar.ics"
    f.write_text(ics)

    intervals = fetch_ics_busy_intervals(
        [f.as_uri()],
        datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert len(intervals) == 1
    assert intervals[0][0] == datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)


def test_ics_busy_blocks_a_match():
    """An ICS-sourced busy interval should drop a conflicting match."""
    from string_theory.conflicts import filter_against_busy
    from zoneinfo import ZoneInfo

    LONDON = ZoneInfo("Europe/London")
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    matches = [make_match(sofa_id=1, start=t, round_short="R32")]
    busy = [(t.astimezone(LONDON) - timedelta(minutes=10),
             t.astimezone(LONDON) + timedelta(minutes=20))]
    out = filter_against_busy(matches, busy)
    assert out == []


def test_improv_show_exception_frees_4_to_6pm_saturday():
    """A Saturday improv-show busy block should leave 16:00–18:00 London free."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import apply_busy_exceptions

    LONDON = ZoneInfo("Europe/London")
    # Sat May 9 2026, 13:00 BST = 12:00 UTC; show event runs 13:00–21:00 BST.
    sat_start_utc = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)   # 13:00 BST
    sat_end_utc = datetime(2026, 5, 9, 20, 0, tzinfo=timezone.utc)     # 21:00 BST

    intervals = apply_busy_exceptions([("Improv show at the Soho", sat_start_utc, sat_end_utc)])
    # Should produce two slices: [13:00 BST, 16:00 BST] and [18:00 BST, 21:00 BST]
    assert len(intervals) == 2
    s0, e0 = intervals[0]
    s1, e1 = intervals[1]
    assert e0.astimezone(LONDON).hour == 16 and e0.astimezone(LONDON).minute == 0
    assert s1.astimezone(LONDON).hour == 18 and s1.astimezone(LONDON).minute == 0


def test_improv_show_exception_only_on_saturday():
    """Same title on a non-Saturday is ignored — no split."""
    from string_theory.conflicts import apply_busy_exceptions
    # Tue May 5 2026 (a weekday)
    s = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    e = datetime(2026, 5, 5, 20, 0, tzinfo=timezone.utc)
    intervals = apply_busy_exceptions([("Improv show", s, e)])
    assert intervals == [(s, e)]


def test_non_matching_title_passes_through():
    from string_theory.conflicts import apply_busy_exceptions
    s = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)  # Saturday
    e = datetime(2026, 5, 9, 20, 0, tzinfo=timezone.utc)
    intervals = apply_busy_exceptions([("Brunch with friends", s, e)])
    assert intervals == [(s, e)]


def test_match_interval_clips_at_bedtime():
    """A match starting at 21:30 BST has its event-block end clipped at 22:30 BST,
    not the natural 00:30 BST that 3h would imply."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import match_interval

    LONDON = ZoneInfo("Europe/London")
    # 21:30 London (BST in May = UTC+1) = 20:30 UTC
    start = datetime(2026, 5, 9, 20, 30, tzinfo=timezone.utc)
    m = make_match(sofa_id=99, start=start, round_short="R32")  # 180 min default
    s, e = match_interval(m)
    end_local = e.astimezone(LONDON)
    assert end_local.hour == 22 and end_local.minute == 30


def test_favorite_wins_tiebreaker_on_overlap():
    """When two overlapping matches tie on score, the one featuring a named
    favorite (favorite_bonus > 0) keeps its slot."""
    t = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    a = replace(make_match(sofa_id=1, start=t, score=9.0),
                score_breakdown={"favorite": 0.0, "total": 9.0})
    b = replace(make_match(sofa_id=2, start=t + timedelta(minutes=10), score=9.0),
                score_breakdown={"favorite": 2.0, "total": 9.0})
    kept = pick_non_overlapping([a, b])
    assert [m.sofa_id for m in kept] == [2]
