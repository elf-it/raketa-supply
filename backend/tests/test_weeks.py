from datetime import date

import pytest

from app.calc.weeks import (
    iso_weeks_in_year,
    parse_week,
    shift_week,
    week_end,
    week_key,
    week_number,
    week_range,
    week_start,
    weeks_between,
)


def test_week_key_matches_isocalendar():
    assert week_key(date(2026, 11, 9)) == "2026-W46"
    assert week_key(date(2026, 11, 15)) == "2026-W46"
    assert week_key(date(2026, 11, 16)) == "2026-W47"


def test_week_start_is_monday_and_end_is_sunday():
    assert week_start("2026-W46") == date(2026, 11, 9)
    assert week_end("2026-W46") == date(2026, 11, 15)
    assert week_start("2026-W46").weekday() == 0


def test_round_trip_through_year_boundary():
    # 2026 год содержит 53 ISO-недели, переход через границу не должен терять недели.
    assert iso_weeks_in_year(2026) == 53
    assert shift_week("2026-W53", 1) == "2027-W01"
    assert shift_week("2027-W01", -1) == "2026-W53"


def test_week_range_and_between_agree():
    assert week_range("2026-W46", 3) == ["2026-W46", "2026-W47", "2026-W48"]
    assert weeks_between("2026-W46", "2026-W48") == ["2026-W46", "2026-W47", "2026-W48"]
    assert weeks_between("2026-W48", "2026-W46") == []


def test_keys_sort_chronologically():
    keys = week_range("2026-W50", 8)
    assert keys == sorted(keys)


def test_week_number_and_parse():
    assert week_number("2026-W07") == 7
    assert parse_week("2026-W07") == (2026, 7)
    with pytest.raises(ValueError):
        parse_week("2026-W7")
