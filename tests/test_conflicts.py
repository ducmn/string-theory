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
    """When busy fully spans the match, drop (no free segment >= MIN_FREE_MINUTES)."""
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    matches = [
        make_match(sofa_id=1, start=t, round_short="R32"),               # busy spans full window
        make_match(sofa_id=2, start=t + timedelta(hours=5)),             # free
    ]
    # R32 ATP block is 180 min. Busy covers all of it.
    busy = [(t - timedelta(minutes=30), t + timedelta(hours=4))]
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
    """A long ICS-sourced busy interval that spans the whole match should drop it."""
    from string_theory.conflicts import filter_against_busy
    from zoneinfo import ZoneInfo

    LONDON = ZoneInfo("Europe/London")
    t = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    matches = [make_match(sofa_id=1, start=t, round_short="R32")]
    # R32 ATP block is 180 min; cover the whole window plus margins.
    busy = [(t.astimezone(LONDON) - timedelta(minutes=10),
             t.astimezone(LONDON) + timedelta(hours=4))]
    out = filter_against_busy(matches, busy)
    assert out == []


def test_apply_busy_exceptions_with_no_rules_passes_through():
    """With BUSY_EXCEPTIONS empty (the default), every event becomes a busy interval verbatim."""
    from string_theory.conflicts import apply_busy_exceptions
    s = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    e = datetime(2026, 5, 9, 20, 0, tzinfo=timezone.utc)
    intervals = apply_busy_exceptions([("Anything", s, e)])
    assert intervals == [(s, e)]


def test_fully_subsumed_drops_only_when_no_free_gap():
    """filter_fully_subsumed: drop iff the whole match window is busy."""
    from string_theory.conflicts import filter_fully_subsumed
    start = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)
    m = make_match(sofa_id=1, start=start, round_short="R32")
    m = replace(m, tour="atp")  # 180 min block: 14:00–17:00 UTC

    # Fully covered → dropped
    fully = [(datetime(2026, 5, 11, 13, 30, tzinfo=timezone.utc),
              datetime(2026, 5, 11, 17, 30, tzinfo=timezone.utc))]
    assert filter_fully_subsumed([m], fully) == []

    # Partial overlap at the front → KEPT, full block, no clipping
    partial = [(datetime(2026, 5, 11, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 5, 11, 14, 30, tzinfo=timezone.utc))]
    out = filter_fully_subsumed([m], partial)
    assert len(out) == 1
    assert out[0].event_clip_start_utc is None  # not clipped

    # Busy entirely inside the match (free at both ends) → KEPT
    middle = [(datetime(2026, 5, 11, 15, 0, tzinfo=timezone.utc),
               datetime(2026, 5, 11, 16, 0, tzinfo=timezone.utc))]
    assert len(filter_fully_subsumed([m], middle)) == 1

    # No busy at all → KEPT
    assert len(filter_fully_subsumed([m], [])) == 1


def test_partial_busy_clips_match_instead_of_dropping():
    """A 30-min meeting at the start of a 3h match should clip, not drop, the event."""
    from string_theory.conflicts import filter_against_busy
    # Match: 14:00–17:00 UTC (3h ATP R32 block)
    match_start = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)
    m = make_match(sofa_id=1, start=match_start, round_short="R32")
    m = replace(m, tour="atp")  # 180 min block

    # Busy: 14:00–14:30 UTC
    busy = [(datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 11, 14, 30, tzinfo=timezone.utc))]

    out = filter_against_busy([m], busy)
    assert len(out) == 1
    kept = out[0]
    # Should have a clip start at 14:30, end at 17:00
    assert kept.event_clip_start_utc == datetime(2026, 5, 11, 14, 30, tzinfo=timezone.utc)
    assert kept.event_clip_end_utc == datetime(2026, 5, 11, 17, 0, tzinfo=timezone.utc)


def test_busy_filter_drops_when_free_segment_too_short():
    """If the only free portion is < 60 min, drop the match."""
    from string_theory.conflicts import filter_against_busy
    match_start = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)
    m = make_match(sofa_id=1, start=match_start, round_short="R32")
    m = replace(m, tour="atp")  # 180 min block 14:00–17:00 UTC

    # Busy 14:00–16:30 UTC — leaves only 16:30–17:00 free (30 min)
    busy = [(datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 11, 16, 30, tzinfo=timezone.utc))]
    out = filter_against_busy([m], busy)
    assert out == []


def test_office_hours_blackout_tue_thu():
    """Tue/Thu 09:00–18:00 London is blanket-skipped regardless of conflicts."""
    from zoneinfo import ZoneInfo
    from string_theory.models import Match, Player
    from string_theory.main import is_in_office_hours

    LONDON = ZoneInfo("Europe/London")
    # Tue May 12 2026, 10:00 BST
    tue_start = datetime(2026, 5, 12, 10, 0, tzinfo=LONDON)
    p = Player(sofa_id=1, full_name="A", short_name="A", country_code="USA", slug="a")
    q = Player(sofa_id=2, full_name="B", short_name="B", country_code="USA", slug="b")
    m_tue = Match(sofa_id=1, tour="atp", tournament_slug="rome", tournament_name="Rome",
                  tournament_tier="M1000", surface="clay", year=2026,
                  round_name="R32", round_short="R32",
                  start_utc=tue_start.astimezone(timezone.utc),
                  player_a=p, player_b=q)
    assert is_in_office_hours(m_tue) is True

    # Wed May 13 2026, 10:00 BST — not an office day
    wed_start = datetime(2026, 5, 13, 10, 0, tzinfo=LONDON)
    m_wed = replace(m_tue, start_utc=wed_start.astimezone(timezone.utc))
    assert is_in_office_hours(m_wed) is False

    # Tue 19:00 BST — past office hours
    tue_eve = datetime(2026, 5, 12, 19, 0, tzinfo=LONDON)
    m_tue_eve = replace(m_tue, start_utc=tue_eve.astimezone(timezone.utc))
    assert is_in_office_hours(m_tue_eve) is False


def test_wta_match_uses_shorter_duration_than_atp():
    """WTA matches (best-of-3) get a 2.5h R32 block; ATP gets 3h."""
    from string_theory.conflicts import match_interval

    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    wta = make_match(sofa_id=1, start=start, round_short="R32")
    wta = replace(wta, tour="wta")
    atp = make_match(sofa_id=2, start=start, round_short="R32")
    atp = replace(atp, tour="atp")

    _, wta_end = match_interval(wta)
    _, atp_end = match_interval(atp)
    assert (wta_end - start).total_seconds() == 150 * 60  # 2.5h
    assert (atp_end - start).total_seconds() == 180 * 60  # 3h


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
