"""Schedules and adherence: hand-counted cases first, then a generated cross-check."""

from datetime import date, datetime, timedelta

import pytest
from django.utils import timezone

from records import models as M
from records import schedule as S

WEEK = (date(2026, 11, 2), date(2026, 11, 8))  # Monday to Sunday
TWO_WEEKS = (date(2026, 11, 2), date(2026, 11, 15))


def count(course, start=WEEK[0], end=WEEK[1]):
    return len(S.expected_doses(course, start, end))


def test_daily_week(course):
    assert count(course()) == 7


def test_twice_daily_is_two_slots_per_day(course):
    assert count(course(schedule={"type": "daily", "times": ["08:00", "20:00"]})) == 14


def test_mid_range_start(course):
    assert count(course(started="2026-11-05")) == 4


def test_stopped_is_inclusive(course):
    assert count(course(stopped="2026-11-04")) == 3


def test_interval_anchored_to_started(course):
    week_plus = (date(2026, 11, 2), date(2026, 11, 16))
    assert count(course(schedule={"type": "interval", "every_days": 14}), *week_plus) == 2


def test_interval_three(course):
    assert count(course(schedule={"type": "interval", "every_days": 3})) == 3


def test_weekly_iso_days(course):
    assert count(course(schedule={"type": "weekly", "days_of_week": [1, 4]})) == 2


def test_cycle_five_on_two_off(course):
    assert count(course(schedule={"type": "cycle", "on_days": 5, "off_days": 2}), *TWO_WEEKS) == 10


def test_prn_expects_nothing(course):
    assert count(course(schedule={"type": "prn", "max_per_day": 2})) == 0


def test_no_times_is_one_slot_at_local_noon(course):
    slots = S.expected_doses(course(schedule={"type": "daily"}), *WEEK)
    assert len(slots) == 7
    assert slots[0]["scheduled_for"].hour == 12


def test_unknown_start_expects_nothing(course):
    assert count(course(started=None)) == 0


def test_stopped_before_range(course):
    assert count(course(stopped="2026-10-30")) == 0


def test_course_change_does_not_double_count(course, user):
    old = course(stopped="2026-11-03")
    new = course(started="2026-11-04")
    assert count(old) + count(new) == 7


def test_dst_spring_forward_keeps_one_slot_per_day(course):
    slots = S.expected_doses(
        course(schedule={"type": "daily", "times": ["02:30"]}, started="2026-03-26"),
        date(2026, 3, 26),
        date(2026, 4, 1),
    )
    assert len(slots) == 7


def test_dst_fall_back_keeps_one_slot_per_day(course):
    slots = S.expected_doses(
        course(schedule={"type": "daily", "times": ["02:30"]}, started="2026-10-22"),
        date(2026, 10, 22),
        date(2026, 10, 28),
    )
    assert len(slots) == 7


def test_two_courses_of_one_substance_cannot_overlap(course, user):
    first = course()
    first.full_clean()
    first.save()
    second = course(started="2026-11-01")
    with pytest.raises(Exception) as error:
        second.full_clean()
    assert "overlaps" in str(error.value)


# --- Adherence ------------------------------------------------------------------------


@pytest.fixture
def saved_course(user):
    def make(**overrides):
        values = {
            "user": user,
            "substance": "Candesartan",
            "schedule": {"type": "daily", "times": ["08:00"]},
            "started": date(2026, 11, 2),
        }
        values.update(overrides)
        return M.Medication._default_manager.create(**values)

    return make


def dose(user, medication, at, status="taken"):
    return M.Dose._default_manager.create(user=user, medication=medication, status=status, at=at)


def local(day, hour=8, minute=0):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))


def test_adherence_two_taken_one_skipped(user, saved_course):
    medication = saved_course()
    dose(user, medication, local(date(2026, 11, 2)))
    dose(user, medication, local(date(2026, 11, 3)))
    dose(user, medication, local(date(2026, 11, 4)), status="skipped")
    result = S.adherence(user, *WEEK)
    assert result == {
        "expected": 7,
        "taken": 2,
        "skipped": 1,
        "unlogged": 4,
        "coverage": 0.43,
    }


def test_adherence_with_no_events_is_zero_not_a_miss(user, saved_course):
    saved_course()
    result = S.adherence(user, *WEEK)
    assert result["taken"] == 0
    assert result["unlogged"] == 7
    assert result["coverage"] == 0.0


def test_extra_events_do_not_cover_a_later_day(user, saved_course):
    medication = saved_course()
    for index in range(3):
        dose(user, medication, local(date(2026, 11, 2), hour=8 + index))
    result = S.adherence(user, *WEEK)
    assert result["taken"] == 3
    assert result["unlogged"] == 6


def test_adherence_prn_has_no_expected_and_null_coverage(user, saved_course):
    saved_course(schedule={"type": "prn", "max_per_day": 2})
    result = S.adherence(user, *WEEK)
    assert result["expected"] == 0
    assert result["coverage"] is None


def test_adherence_stopped_before_range_is_empty(user, saved_course):
    saved_course(started=date(2026, 10, 1), stopped=date(2026, 10, 30))
    result = S.adherence(user, *WEEK)
    assert result["expected"] == 0 and result["taken"] == 0


def test_event_at_2330_local_counts_on_its_own_day(user, saved_course):
    medication = saved_course()
    dose(user, medication, local(date(2026, 11, 2), hour=23, minute=30))
    dose(user, medication, local(date(2026, 11, 3), hour=7))
    assert S.adherence(user, *WEEK)["taken"] == 2


def test_twice_daily_week(user, saved_course):
    medication = saved_course(schedule={"type": "daily", "times": ["08:00", "20:00"]})
    dose(user, medication, local(date(2026, 11, 2)))
    result = S.adherence(user, *WEEK)
    assert result["expected"] == 14
    assert result["unlogged"] == 13


# --- Generated cross-check ------------------------------------------------------------


def naive_expected_days(schedule, started, start, end, stopped=None):
    """Independent flat loop; no shared helper with the implementation under test."""
    if not schedule or started is None:
        return []
    days = []
    day = start
    while day <= end:
        if day >= started and (stopped is None or day <= stopped):
            kind = schedule["type"]
            offset = (day - started).days
            if kind == "daily":
                days.append(day)
            elif kind == "interval" and offset % schedule["every_days"] == 0:
                days.append(day)
            elif kind == "weekly" and day.isoweekday() in schedule["days_of_week"]:
                days.append(day)
            elif (
                kind == "cycle"
                and offset % (schedule["on_days"] + schedule["off_days"]) < (schedule["on_days"])
            ):
                days.append(day)
        day += timedelta(days=1)
    return days


@pytest.mark.parametrize("seed", range(200))
def test_generated_schedules_match_naive_loop(seed, course):
    import random

    rng = random.Random(seed)
    kind = rng.choice(["daily", "interval", "weekly", "cycle", "prn"])
    schedule = {"type": kind}
    if kind == "interval":
        schedule["every_days"] = rng.randint(1, 30)
    if kind == "weekly":
        schedule["days_of_week"] = sorted(rng.sample(range(1, 8), rng.randint(1, 3)))
    if kind == "cycle":
        schedule["on_days"] = rng.randint(1, 10)
        schedule["off_days"] = rng.randint(0, 10)
    started = date(2026, 1, 1) + timedelta(days=rng.randint(0, 60))
    stopped = None if rng.random() < 0.5 else started + timedelta(days=rng.randint(0, 400))
    start = started + timedelta(days=rng.randint(0, 30))
    end = start + timedelta(days=rng.randint(0, 730))
    course(schedule=schedule, started=started, stopped=stopped)
    got = S.due_dates(schedule, started, start, end, stopped)
    assert got == naive_expected_days(schedule, started, start, end, stopped)
