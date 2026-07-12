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


def test_stack_sequential_same_tournament_matches():
    """Two same-tournament matches back-to-back: the first keeps its full block;
    the second is pushed to start when the first ends (no overlap, full time)."""
    from string_theory.conflicts import stack_sequential_matches

    # Same slug ("rome") = same tournament. ATP SF block is 165 min.
    s1 = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    s2 = datetime(2026, 5, 9, 13, 40, tzinfo=timezone.utc)  # before m1's block ends
    m1 = replace(make_match(sofa_id=1, start=s1, round_short="SF"), tour="atp")
    m2 = replace(make_match(sofa_id=2, start=s2, round_short="SF"), tour="atp")

    out = {m.sofa_id: m for m in stack_sequential_matches([m1, m2])}
    m1_end = datetime(2026, 5, 9, 14, 45, tzinfo=timezone.utc)   # s1 + 165 min
    # m1 keeps its natural block (first in the group) — no clip applied
    assert out[1].event_clip_start_utc is None
    assert out[1].event_clip_end_utc is None
    # m2 is pushed to start at m1's end and keeps a full 165-min block
    assert out[2].event_clip_start_utc == m1_end
    assert out[2].event_clip_end_utc == m1_end + timedelta(minutes=165)


def test_split_creates_resume_block_on_mid_conflict():
    """A busy event in the middle of a match splits it into two blocks: the
    part before (part 1) and a resume block after (part 2)."""
    from string_theory.conflicts import split_matches_around_busy
    start = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    m = replace(make_match(sofa_id=1, start=start, round_short="F"), tour="atp")  # 180 min
    busy = [(datetime(2026, 5, 9, 15, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 9, 15, 30, tzinfo=timezone.utc))]
    out = split_matches_around_busy([m], busy)
    assert len(out) == 2
    assert out[0].part == 1
    assert out[0].event_clip_start_utc == start
    assert out[0].event_clip_end_utc == datetime(2026, 5, 9, 15, 0, tzinfo=timezone.utc)
    assert out[1].part == 2
    assert out[1].event_clip_start_utc == datetime(2026, 5, 9, 15, 30, tzinfo=timezone.utc)
    assert out[1].event_clip_end_utc == datetime(2026, 5, 9, 17, 0, tzinfo=timezone.utc)


def test_split_exempts_favorites():
    """A favorite match is never cut or split — runs its full block."""
    from string_theory.conflicts import split_matches_around_busy
    start = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    m = replace(make_match(sofa_id=1, start=start, round_short="F"), tour="atp")
    m = replace(m, score_breakdown={"favorite": 2.0})
    busy = [(datetime(2026, 5, 9, 15, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 9, 15, 30, tzinfo=timezone.utc))]
    out = split_matches_around_busy([m], busy)
    assert len(out) == 1
    assert out[0].event_clip_start_utc is None  # untouched


def test_favorite_yields_to_work_calendar():
    """A favorite match is NOT exempt from the work calendar: a work meeting
    fully covering its window drops it, unlike a personal event."""
    from string_theory.conflicts import split_matches_around_busy
    start = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    m = replace(make_match(sofa_id=1, start=start, round_short="F"), tour="atp")  # 180 min
    m = replace(m, score_breakdown={"favorite": 2.0})
    work = [(datetime(2026, 5, 9, 13, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc))]
    # As personal busy it would be ignored; as work busy it drops the favorite.
    assert len(split_matches_around_busy([m], work)) == 1
    assert split_matches_around_busy([m], [], work_busy=work) == []


def test_favorite_still_ignores_personal_but_splits_around_work():
    """A favorite ignores a personal event but is cut short around a work one."""
    from string_theory.conflicts import split_matches_around_busy
    start = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    m = replace(make_match(sofa_id=1, start=start, round_short="F"), tour="atp")  # 180 min
    m = replace(m, score_breakdown={"favorite": 2.0})
    personal = [(datetime(2026, 5, 9, 14, 30, tzinfo=timezone.utc),
                 datetime(2026, 5, 9, 15, 0, tzinfo=timezone.utc))]
    work = [(datetime(2026, 5, 9, 16, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 9, 17, 0, tzinfo=timezone.utc))]
    out = split_matches_around_busy([m], personal, work_busy=work)
    # Personal event ignored; only the work event cuts the block to 14:00–16:00.
    assert len(out) == 1
    assert out[0].event_clip_start_utc == start
    assert out[0].event_clip_end_utc == datetime(2026, 5, 9, 16, 0, tzinfo=timezone.utc)


def test_pick_non_overlapping_prefers_tennis_over_football():
    """When a tennis and a football match heavily overlap, tennis wins even
    though football scores higher."""
    from string_theory.conflicts import pick_non_overlapping
    start = datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc)
    tennis = replace(make_match(sofa_id=1, start=start, round_short="SF"), tour="atp", score=9.0)
    football = Match(
        sofa_id=2, tour="football", tournament_slug="world-championship",
        tournament_name="FIFA World Cup", tournament_tier="FOOTBALL",
        surface="", year=2026, round_name="Quarterfinals", round_short="Quarterfinals",
        start_utc=start, score=12.0,
        player_a=Player(sofa_id=1, full_name="Spain", short_name="Spain",
                        country_code="", slug="spain", ranking=None),
        player_b=Player(sofa_id=2, full_name="Belgium", short_name="Belgium",
                        country_code="", slug="belgium", ranking=None),
    )
    kept = pick_non_overlapping([football, tennis])
    assert len(kept) == 1
    assert kept[0].sofa_id == 1  # the tennis match


def test_filter_no_overlap_drops_on_any_overlap():
    """filter_no_overlap drops a match that overlaps a busy interval even
    partially — stricter than filter_fully_subsumed."""
    from string_theory.conflicts import filter_no_overlap
    match_start = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)
    m = make_match(sofa_id=1, start=match_start, round_short="R32")
    m = replace(m, tour="atp")  # 14:00–16:00 UTC block

    # A 15-min clash near the end still drops the whole match.
    tail_clash = [(datetime(2026, 5, 11, 15, 45, tzinfo=timezone.utc),
                   datetime(2026, 5, 11, 16, 0, tzinfo=timezone.utc))]
    assert filter_no_overlap([m], tail_clash) == []

    # A non-overlapping busy interval keeps it.
    elsewhere = [(datetime(2026, 5, 11, 16, 0, tzinfo=timezone.utc),
                  datetime(2026, 5, 11, 17, 0, tzinfo=timezone.utc))]
    assert len(filter_no_overlap([m], elsewhere)) == 1

    # No busy → kept.
    assert len(filter_no_overlap([m], [])) == 1


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
    # Should have a clip start at 14:30, end at 16:00
    assert kept.event_clip_start_utc == datetime(2026, 5, 11, 14, 30, tzinfo=timezone.utc)
    assert kept.event_clip_end_utc == datetime(2026, 5, 11, 16, 0, tzinfo=timezone.utc)


def test_busy_filter_drops_when_free_segment_too_short():
    """If the only free portion is < 60 min, drop the match."""
    from string_theory.conflicts import filter_against_busy
    match_start = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)
    m = make_match(sofa_id=1, start=match_start, round_short="R32")
    m = replace(m, tour="atp")  # 120 min block 14:00–16:00 UTC

    # Busy 14:00–15:30 UTC — leaves only 15:30–16:00 free (30 min)
    busy = [(datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 11, 15, 30, tzinfo=timezone.utc))]
    out = filter_against_busy([m], busy)
    assert out == []


def test_office_hours_matches_allowed_when_free():
    """Office-hours matches are no longer blanket-blocked — a Tue daytime match
    with no work-calendar clash is kept; conflicts are handled per-meeting."""
    from zoneinfo import ZoneInfo
    from string_theory.main import select_matches

    LONDON = ZoneInfo("Europe/London")
    # Tue May 12 2026, 10:00 BST — an office-hours slot, but no busy check here.
    tue_start = datetime(2026, 5, 12, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    m = replace(make_match(sofa_id=1, start=tue_start, round_short="F"),
                score_breakdown={"favorite": 2.0}, score=8.0)
    kept = select_matches([m])
    assert [x.sofa_id for x in kept] == [1]


def test_wta_match_uses_shorter_duration_than_atp():
    """WTA matches (best-of-3) get a shorter R32 block than ATP."""
    from string_theory.conflicts import match_interval

    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    wta = make_match(sofa_id=1, start=start, round_short="R32")
    wta = replace(wta, tour="wta")
    atp = make_match(sofa_id=2, start=start, round_short="R32")
    atp = replace(atp, tour="atp")

    _, wta_end = match_interval(wta)
    _, atp_end = match_interval(atp)
    assert (wta_end - start).total_seconds() == 105 * 60  # 1h45
    assert (atp_end - start).total_seconds() == 120 * 60  # 2h


def test_match_interval_clips_at_bedtime():
    """A non-favorite match starting at 21:30 BST has its event-block end
    clipped at 23:00 BST, not the natural end that its duration would imply."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import match_interval

    LONDON = ZoneInfo("Europe/London")
    # 21:30 London (BST in May = UTC+1) = 20:30 UTC
    start = datetime(2026, 5, 9, 20, 30, tzinfo=timezone.utc)
    m = make_match(sofa_id=99, start=start, round_short="SF")  # 165 min -> 00:15, clipped
    s, e = match_interval(m)
    end_local = e.astimezone(LONDON)
    assert end_local.hour == 23 and end_local.minute == 0


def test_favorite_match_runs_past_bedtime():
    """A favorite (England / a favorite player) is NOT clipped at bedtime — the
    block runs to its natural end so the user can watch to the finish."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import match_interval
    from string_theory.score import score_match

    LONDON = ZoneInfo("Europe/London")
    # England World Cup QF starting 22:00 London (BST) = 21:00 UTC; 150 min
    # football block => 00:30 London, which must NOT be clipped to 23:00.
    start = datetime(2026, 7, 11, 21, 0, tzinfo=timezone.utc)
    m = Match(
        sofa_id=7, tour="football", tournament_slug="world-championship",
        tournament_name="FIFA World Cup", tournament_tier="FOOTBALL",
        surface="", year=2026, round_name="Quarterfinals", round_short="Quarterfinals",
        start_utc=start,
        player_a=Player(sofa_id=1, full_name="Norway", short_name="Norway",
                        country_code="", slug="norway", ranking=None),
        player_b=Player(sofa_id=2, full_name="England", short_name="England",
                        country_code="", slug="england", ranking=None),
    )
    m = score_match(m)  # sets favorite bonus
    _, e = match_interval(m)
    end_local = e.astimezone(LONDON)
    assert (end_local.hour, end_local.minute) == (0, 30)  # full block, past bedtime


def test_favorite_always_wins_overlap_even_against_higher_score():
    """A favorite match wins overlap dedup regardless of score gap."""
    t = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    # Non-favorite, high score
    a = replace(make_match(sofa_id=1, start=t, score=10.0),
                score_breakdown={"favorite": 0.0, "total": 10.0})
    # Favorite, lower score, overlapping start
    b = replace(make_match(sofa_id=2, start=t + timedelta(minutes=30), score=6.0),
                score_breakdown={"favorite": 2.0, "total": 6.0})
    kept = pick_non_overlapping([a, b])
    assert [m.sofa_id for m in kept] == [2]


def test_tail_overlap_keeps_both_matches():
    """A 20-min tail overlap between Iga (11:00–12:30) and Rybakina (12:10–13:40)
    is below the heavy-overlap threshold — both matches stay."""
    iga_start = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)  # 11:00 BST in May
    ryb_start = datetime(2026, 5, 25, 11, 10, tzinfo=timezone.utc)  # 12:10 BST, 70 min later
    iga = replace(make_match(sofa_id=1, start=iga_start, score=7.0),
                  tour="wta", round_short="R128",
                  score_breakdown={"favorite": 0.0, "total": 7.0})
    ryb = replace(make_match(sofa_id=2, start=ryb_start, score=8.0),
                  tour="wta", round_short="R128",
                  score_breakdown={"favorite": 0.0, "total": 8.0})
    # WTA R128 = 90 min blocks. Iga 10:00–11:30 UTC, Rybakina 11:10–12:40 UTC.
    # Overlap = 11:10–11:30 = 20 min. Shorter duration = 90 min. Ratio = 22% < 50%.
    kept = pick_non_overlapping([iga, ryb])
    assert {m.sofa_id for m in kept} == {1, 2}


def test_heavy_overlap_drops_lower_scored():
    """A 60-min overlap between two 90-min matches crosses the 50% threshold."""
    a_start = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
    b_start = datetime(2026, 5, 25, 10, 30, tzinfo=timezone.utc)  # 30 min later
    a = replace(make_match(sofa_id=1, start=a_start, score=7.0),
                tour="wta", round_short="R128",
                score_breakdown={"favorite": 0.0, "total": 7.0})
    b = replace(make_match(sofa_id=2, start=b_start, score=8.0),
                tour="wta", round_short="R128",
                score_breakdown={"favorite": 0.0, "total": 8.0})
    # Block a: 10:00–11:30, block b: 10:30–12:00. Overlap = 10:30–11:30 = 60 min.
    # Ratio = 60/90 = 67% > 50%. Lower-scored (a) gets dropped.
    kept = pick_non_overlapping([a, b])
    assert [m.sofa_id for m in kept] == [2]


def test_among_non_favorites_higher_score_wins():
    """Two non-favorite overlapping matches: highest score wins."""
    t = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    lower = replace(make_match(sofa_id=1, start=t, score=7.0),
                    score_breakdown={"favorite": 0.0, "total": 7.0})
    higher = replace(make_match(sofa_id=2, start=t + timedelta(minutes=30), score=11.0),
                     score_breakdown={"favorite": 0.0, "total": 11.0})
    kept = pick_non_overlapping([lower, higher])
    assert [m.sofa_id for m in kept] == [2]


# --- Recurring ICS meeting expansion ------------------------------------------

def test_iter_occurrences_weekly_expands_future_weeks():
    """A weekly meeting blocks a date months after its first occurrence."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import _iter_occurrences
    LON = ZoneInfo("Europe/London")
    # Weekly Tue 11:00-12:00 starting long before the window.
    start = datetime(2026, 1, 6, 11, 0, tzinfo=LON)   # a Tuesday
    end = datetime(2026, 1, 6, 12, 0, tzinfo=LON)
    w_min = datetime(2026, 7, 14, 0, 0, tzinfo=LON)   # a Tuesday, months later
    w_max = datetime(2026, 7, 15, 0, 0, tzinfo=LON)
    occ = list(_iter_occurrences(start, end, "FREQ=WEEKLY;UNTIL=20270706T060000Z",
                                 set(), w_min, w_max))
    assert len(occ) == 1
    assert occ[0][0] == datetime(2026, 7, 14, 11, 0, tzinfo=LON)
    assert occ[0][1] == datetime(2026, 7, 14, 12, 0, tzinfo=LON)


def test_iter_occurrences_until_stops_series():
    """No occurrence is emitted after the UNTIL bound."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import _iter_occurrences
    LON = ZoneInfo("Europe/London")
    start = datetime(2026, 1, 6, 11, 0, tzinfo=LON)
    end = datetime(2026, 1, 6, 12, 0, tzinfo=LON)
    # Series ends in April; a July window sees nothing.
    occ = list(_iter_occurrences(start, end, "FREQ=WEEKLY;UNTIL=20260414T100000Z",
                                 set(), datetime(2026, 7, 14, tzinfo=LON),
                                 datetime(2026, 7, 15, tzinfo=LON)))
    assert occ == []


def test_iter_occurrences_exdate_skips_cancelled():
    """An EXDATE cancels that week's occurrence."""
    from zoneinfo import ZoneInfo
    from string_theory.conflicts import _iter_occurrences
    LON = ZoneInfo("Europe/London")
    start = datetime(2026, 7, 7, 11, 0, tzinfo=LON)   # Tue
    end = datetime(2026, 7, 7, 12, 0, tzinfo=LON)
    ex = {datetime(2026, 7, 14, 11, 0)}               # cancel the 14th
    occ = list(_iter_occurrences(start, end, "FREQ=WEEKLY", ex,
                                 datetime(2026, 7, 14, tzinfo=LON),
                                 datetime(2026, 7, 15, tzinfo=LON)))
    assert occ == []
