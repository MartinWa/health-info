"""Derived values: expected dose slots and adherence, both from the course schedule."""

from collections import Counter
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from records.models import Dose, Medication, Person


def account_timezone(user):
    zone = Person.objects.filter(user=user).values_list("timezone", flat=True).first()
    try:
        return ZoneInfo(zone or settings.TIME_ZONE)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo(settings.TIME_ZONE)


def local_day_bounds(day, zone):
    """[00:00, 24:00) local for one calendar day, as aware datetimes."""
    start = datetime.combine(day, time(0, 0), tzinfo=zone)
    return start, start + timedelta(days=1)


def due_dates(schedule, started, start, end, stopped=None):
    """Days the course expects a dose in [start, end]; a change on D means old serves to D-1."""
    if not schedule or started is None:
        return []
    first = max(start, started)
    last = min(end, stopped) if stopped else end
    if first > last:
        return []
    days = []
    day = first
    while day <= last:
        if is_due_day(schedule, started, day):
            days.append(day)
        day += timedelta(days=1)
    return days


def is_due_day(schedule, started, day):
    kind = schedule.get("type")
    if kind == "daily":
        return True
    if kind == "interval":
        return (day - started).days % max(schedule.get("every_days", 1), 1) == 0
    if kind == "weekly":
        return day.isoweekday() in schedule.get("days_of_week", [])
    if kind == "cycle":
        on_days = schedule.get("on_days", 1)
        off_days = schedule.get("off_days", 0)
        period = on_days + off_days
        return period > 0 and (day - started).days % period < on_days
    return False


def slot_times(day, schedule, zone):
    """One slot per entry in times, or local noon when the time is unknown."""
    times = schedule.get("times") or []
    if not times:
        return [datetime.combine(day, time(12, 0), tzinfo=zone)]
    return [datetime.combine(day, time.fromisoformat(value), tzinfo=zone) for value in times]


def expected_doses(course, start, end):
    """One element per (date, course, slot), the row shape /expected-doses returns."""
    zone = account_timezone(course.user)
    slots = []
    for day in due_dates(course.schedule, course.started, start, end, course.stopped):
        for moment in slot_times(day, course.schedule, zone):
            slots.append(
                {
                    "medication_id": course.pk,
                    "substance": course.substance,
                    "strength": course.strength,
                    "unit": course.unit,
                    "date": day.isoformat(),
                    "scheduled_for": moment,
                }
            )
    return slots


def expected_doses_for_user(user, start, end, medication=None):
    courses = Medication._default_manager.filter(user=user, started__lte=end).filter(
        Q(stopped__isnull=True) | Q(stopped__gte=start)
    )
    if medication is not None:
        courses = courses.filter(pk=medication.pk)
    slots = []
    for course in courses:
        slots.extend(expected_doses(course, start, end))
    return sorted(slots, key=lambda slot: slot["scheduled_for"])


def planned_slots(user, start, end):
    """Today's expected doses with the logged status of each slot, for the UI."""
    zone = account_timezone(user)
    first, _ = local_day_bounds(start, zone)
    _, after_last = local_day_bounds(end, zone)
    # Two aware datetimes with equal instants can compare unequal across a DST fold, so key
    # the lookup on UTC.
    events = {
        (event.medication_id, event.scheduled_for.astimezone(UTC)): event.status
        for event in Dose._default_manager.filter(
            user=user, scheduled_for__gte=first, scheduled_for__lt=after_last
        )
    }
    rows = []
    for slot in expected_doses_for_user(user, start, end):
        key = (slot["medication_id"], slot["scheduled_for"].astimezone(UTC))
        rows.append({**slot, "status": events.get(key)})
    return rows


def adherence(user, start, end, medication=None):
    """Per local day: taken and skipped count on the day of `at`; unlogged is not a miss."""
    zone = account_timezone(user)
    slots = expected_doses_for_user(user, start, end, medication=medication)
    expected_by_day = Counter(slot["date"] for slot in slots)
    first, _ = local_day_bounds(start, zone)
    _, after_last = local_day_bounds(end, zone)
    events = Dose._default_manager.filter(user=user, at__gte=first, at__lt=after_last)
    if medication is not None:
        events = events.filter(medication=medication)
    taken_by_day = Counter()
    skipped_by_day = Counter()
    for event in events:
        day = timezone.localtime(event.at, zone).date().isoformat()
        if event.status == "taken":
            taken_by_day[day] += 1
        else:
            skipped_by_day[day] += 1
    expected = sum(expected_by_day.values())
    taken = sum(taken_by_day.values())
    skipped = sum(skipped_by_day.values())
    unlogged = sum(
        max(0, count - taken_by_day[day] - skipped_by_day[day])
        for day, count in expected_by_day.items()
    )
    coverage = round((taken + skipped) / expected, 2) if expected else None
    return {
        "expected": expected,
        "taken": taken,
        "skipped": skipped,
        "unlogged": unlogged,
        "coverage": coverage,
    }


def describe_schedule(schedule):
    """Short human text for a schedule object, never a raw JSON dump."""
    if not schedule:
        return "no schedule"
    kind = schedule.get("type")
    times = schedule.get("times") or []
    at = f" at {', '.join(times)}" if times else ""
    if kind == "daily":
        return "daily" + at
    if kind == "interval":
        return f"every {schedule.get('every_days')} days" + at
    if kind == "weekly":
        days = ", ".join(str(day) for day in schedule.get("days_of_week", []))
        return f"weekly on {days}" + at
    if kind == "cycle":
        return f"{schedule.get('on_days')} on / {schedule.get('off_days')} off" + at
    if kind == "prn":
        return f"as needed, max {schedule.get('max_per_day')} per day"
    return kind or "no schedule"
