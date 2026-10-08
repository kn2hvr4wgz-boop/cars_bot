from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bot.calc import Rates, calc_shift, money, overtime_hours
from bot.parse import combine, parse_date, parse_number, parse_time

TZ = ZoneInfo("Europe/Moscow")
RATES = Rates(shift_rate=6000, shift_hours=12, overtime_rate=600, overtime_step_min=60,
              km_included=150, km_rate=15)


def at(h, m=0, day=6):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def test_plain_shift():
    c = calc_shift(at(8), at(20), 120, [], RATES)
    assert c.hours == 12
    assert c.overtime_hours == 0
    assert c.km_pay == 0
    assert c.total == 6000


def test_overtime_rounds_up_to_step():
    c = calc_shift(at(8), at(21, 10), 0, [], RATES)
    assert c.overtime_hours == 2
    assert c.overtime_pay == 1200
    assert overtime_hours(12.25, Rates(shift_hours=12, overtime_step_min=30)) == 0.5
    assert overtime_hours(12.25, Rates(shift_hours=12, overtime_step_min=0)) == 0.25


def test_km_and_expenses():
    c = calc_shift(at(8), at(20), 200, [("toll", 450), ("parking", 300), ("toll", 150)], RATES)
    assert c.km_over == 50
    assert c.km_pay == 750
    assert c.expenses["toll"] == 600
    assert c.total == 6000 + 750 + 900


def test_open_shift_has_no_pay_yet():
    c = calc_shift(at(8), None, None, [("parking", 100)], RATES)
    assert c.shift_pay == 0 and c.total == 100


def test_member_overrides():
    project = {"shift_rate": 6000, "shift_hours": 12, "overtime_rate": 600,
               "overtime_step_min": 60, "km_included": 0, "km_rate": 0}
    member = {"shift_rate": 8000, "shift_hours": None, "overtime_rate": None,
              "overtime_step_min": None, "km_included": None, "km_rate": None}
    r = Rates.merged(project, member)
    assert r.shift_rate == 8000 and r.overtime_rate == 600


def test_parsers():
    assert parse_number("1 250,50") == 1250.5
    assert parse_number("450р") == 450
    assert parse_number("abc") is None
    assert parse_time("8") == time(8)
    assert parse_time("07.30") == time(7, 30)
    assert parse_time("2115") == time(21, 15)
    assert parse_time("25:00") is None
    today = date(2026, 1, 10)
    assert parse_date("вчера", today) == date(2026, 1, 9)
    assert parse_date("28.12", today) == date(2025, 12, 28)
    assert parse_date("5.1.26", today) == date(2026, 1, 5)
    assert money(12345) == "12 345 ₽"


def test_overnight_end():
    start = combine(date(2026, 10, 6), time(18), TZ)
    end = combine(date(2026, 10, 6), time(2), TZ, after=start)
    assert end == at(2, day=7)
