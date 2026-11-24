"""Dashboard lines and the overview cards: vitals, today's doses, recent activity."""

from datetime import datetime

from django.urls import reverse
from django.utils import timezone

from records import models as M

DOMAIN_LABELS = {
    "identity": "Person",
    "environment": "Environment",
    "goals": "Goals",
    "program": "Programme",
    "conditions": "Conditions",
    "injuries": "Injuries",
    "allergies": "Allergies",
    "medications": "Medications",
    "measurements": "Measurements",
    "symptoms": "Symptoms",
    "checkins": "Check-ins",
    "labs": "Labs",
    "vaccinations": "Vaccinations",
    "appointments": "Appointments",
    "notes": "Notes",
    "memory": "Memory",
}


def _count(model, user, **filters):
    return model._default_manager.filter(user=user, **filters).count()


# --- Overview cards --------------------------------------------------------------------


# Consumer-facing bands, not clinical guidance: source is the US ACC/AHA pressure categories.
def _status(metric, value):
    if value is None:
        return None
    if metric == "pulse":
        return "normal" if 60 <= value <= 100 else "borderline" if 50 <= value <= 110 else "out"
    return None


def _bp_status(systolic, diastolic):
    if systolic is None or diastolic is None:
        return None
    if systolic >= 140 or diastolic >= 90:
        return "out"
    if systolic >= 120 or diastolic >= 80:
        return "borderline"
    return "normal"


STATUS_LABELS = {"normal": "In range", "borderline": "Borderline", "out": "Out of range"}


def _sparkline(values):
    """SVG polyline points in a 100x32 box; empty when there is nothing to draw."""
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = 100 / (len(values) - 1)
    return " ".join(
        f"{index * step:.1f},{30 - (value - low) / span * 24:.1f}"
        for index, value in enumerate(values)
    )


def _series(queryset, limit=14):
    values = [float(row.value) for row in queryset[:limit] if row.value is not None]
    return list(reversed(values))


def vitals(user):
    """Latest reading per headline metric plus a short series for its sparkline."""
    components = M.Measurement._default_manager.filter(
        user=user, metric__in=["systolic_bp", "diastolic_bp"], parent__isnull=False
    )
    systolic = components.filter(metric="systolic_bp").order_by("-at")
    diastolic = components.filter(metric="diastolic_bp").order_by("-at")
    latest_bp = systolic.first()
    latest_diastolic = diastolic.first()
    pulse = M.Measurement._default_manager.filter(user=user, metric="pulse").order_by("-at")
    weight = M.Measurement._default_manager.filter(user=user, metric="body_weight").order_by("-at")
    latest_pulse, latest_weight = pulse.first(), weight.first()

    def card(code, label, value, unit, status, at, values):
        return {
            "code": code,
            "label": label,
            "value": value,
            "unit": unit,
            "status": status,
            "status_label": STATUS_LABELS.get(status, ""),
            "at": at,
            "sparkline": _sparkline(values),
        }

    systolic_now = latest_bp.value if latest_bp else None
    diastolic_now = latest_diastolic.value if latest_diastolic else None
    return [
        card(
            "blood_pressure",
            "Blood pressure",
            f"{_number(systolic_now)}/{_number(diastolic_now)}"
            if systolic_now is not None and diastolic_now is not None
            else "—",
            "mmHg",
            _bp_status(systolic_now, diastolic_now),
            latest_bp.at if latest_bp else None,
            _series(systolic),
        ),
        card(
            "pulse",
            "Pulse",
            _number(latest_pulse.value if latest_pulse else None),
            "/min",
            _status("pulse", float(latest_pulse.value) if latest_pulse else None),
            latest_pulse.at if latest_pulse else None,
            _series(pulse),
        ),
        card(
            "body_weight",
            "Weight",
            _number(latest_weight.value if latest_weight else None),
            "kg",
            None,
            latest_weight.at if latest_weight else None,
            _series(weight),
        ),
    ]


def _number(value):
    if value is None:
        return "—"
    return format(value.normalize(), "f") if hasattr(value, "normalize") else f"{value:g}"


def dose_groups(slots):
    """Today's slots by time of day, each with its progress, for the checklist."""
    bands = (("Morning", 0, 12), ("Afternoon", 12, 17), ("Evening", 17, 22), ("Night", 22, 24))
    groups = []
    for label, start, end in bands:
        rows = [
            slot for slot in slots if start <= timezone.localtime(slot["scheduled_for"]).hour < end
        ]
        if not rows:
            continue
        groups.append(
            {
                "label": label,
                "rows": rows,
                "taken": sum(1 for slot in rows if slot["status"] == "taken"),
                "total": len(rows),
            }
        )
    return groups


def recent_activity(user, limit=6):
    """Newest rows across the logged kinds, the same items the timeline lists."""
    items = []

    def add(queryset, kind, text, url_name, when_field="at"):
        for obj in queryset:
            items.append(
                {
                    "at": getattr(obj, when_field),
                    "kind": kind,
                    "text": text(obj),
                    "url": reverse(url_name, args=[obj.pk]) if url_name else None,
                }
            )

    add(
        M.Measurement._default_manager.filter(user=user, parent__isnull=True),
        "Measurement",
        _measurement_text,
        "records:measurements_detail",
    )
    add(
        M.Symptom._default_manager.filter(user=user),
        "Symptom",
        lambda obj: obj.name,
        "records:symptoms_detail",
        "onset_at",
    )
    add(
        M.Checkin._default_manager.filter(user=user),
        "Check-in",
        lambda obj: "Check-in",
        "records:checkins_detail",
    )
    add(
        M.Note._default_manager.filter(user=user),
        "Note",
        lambda obj: obj.body[:80],
        "records:notes_detail",
    )
    add(
        M.DoseEvent._default_manager.filter(user=user),
        "Dose",
        lambda obj: f"{obj.medication} {obj.status}",
        None,
    )
    for period in M.Period._default_manager.filter(user=user):
        items.append(
            {
                "at": timezone.make_aware(datetime.combine(period.start, datetime.min.time())),
                "kind": "Period",
                "text": f"{period.get_kind_display()} {period.start}",
                "url": reverse("records:periods_detail", args=[period.pk]),
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]


def _title(code):
    return code.replace("_", " ").capitalize()


def _measurement_text(obj):
    """A blood pressure panel stores no value of its own; read its two components."""
    if obj.metric == "blood_pressure":
        parts = {part.metric: part.value for part in obj.components.all()}
        if "systolic_bp" in parts and "diastolic_bp" in parts:
            systolic, diastolic = _number(parts["systolic_bp"]), _number(parts["diastolic_bp"])
            return f"Blood pressure {systolic}/{diastolic}"
    return f"{_title(obj.metric)} {_number(obj.value)} {obj.unit}".strip()


def _last(value):
    return timezone.localtime(value).strftime("%Y-%m-%d") if value else "none yet"


def domain_lines(user):
    person = M.Person._default_manager.filter(user=user).first()
    environment = M.Environment._default_manager.filter(user=user).first()
    program = (
        M.Program._default_manager.filter(user=user).order_by("-started", "-created_at").first()
    )
    goals = list(M.Goal._default_manager.filter(user=user, status="active"))
    next_appointment = (
        M.Appointment._default_manager.filter(user=user, at__gte=timezone.now(), status="planned")
        .order_by("at")
        .first()
    )
    last_measurement = (
        M.Measurement._default_manager.filter(user=user, parent__isnull=True)
        .order_by("-at")
        .first()
    )
    last_symptom = M.Symptom._default_manager.filter(user=user).order_by("-onset_at").first()
    last_checkin = M.Checkin._default_manager.filter(user=user).order_by("-at").first()

    def summary_of_identity():
        if person is None or not (person.name or person.birth_date):
            return "Name and birth date not recorded"
        age = person.age_years if person.birth_date else "?"
        return f"{age} years · {person.get_sex_at_birth_display()}"

    lines = [
        ("identity", summary_of_identity(), "records:person", "Edit"),
        (
            "environment",
            " · ".join(
                part
                for part in (
                    environment.get_gym_access_display()
                    if environment and environment.gym_access
                    else "",
                    environment.get_work_pattern_display() if environment else "",
                )
                if part
            )
            or "Not recorded",
            "records:environment",
            "Edit",
        ),
        (
            "goals",
            f"{len(goals)} active" + (f" · {goals[0].metric}" if goals else ""),
            "records:goals_list",
            "Goals",
        ),
        (
            "program",
            f"{program.name} · {program.phase}" if program else "No programme",
            "records:program",
            "Edit",
        ),
        (
            "conditions",
            f"{_count(M.Condition, user, status='active')} active",
            "records:conditions_list",
            "View",
        ),
        (
            "injuries",
            f"{_count(M.Injury, user, status='active')} active",
            "records:injuries_list",
            "View",
        ),
        ("allergies", f"{_count(M.Allergy, user)} recorded", "records:allergies_list", "View"),
        (
            "medications",
            f"{_count(M.Medication, user, stopped__isnull=True)} active course"
            + ("s" if _count(M.Medication, user, stopped__isnull=True) != 1 else ""),
            "records:regimen",
            "Regimen",
        ),
        (
            "measurements",
            f"last {_last(last_measurement.at if last_measurement else None)}",
            "records:measurements_list",
            "View",
        ),
        (
            "symptoms",
            f"last {_last(last_symptom.onset_at if last_symptom else None)}",
            "records:symptoms_list",
            "View",
        ),
        (
            "checkins",
            f"last {_last(last_checkin.at if last_checkin else None)}",
            "records:checkins_list",
            "View",
        ),
        ("labs", f"{_count(M.Lab, user)} results", "records:labs_list", "View"),
        (
            "vaccinations",
            f"{_count(M.Vaccination, user)} recorded",
            "records:vaccinations_list",
            "View",
        ),
        (
            "appointments",
            f"next {timezone.localtime(next_appointment.at).strftime('%Y-%m-%d %H:%M')}"
            if next_appointment
            else "none planned",
            "records:appointments_list",
            "View",
        ),
        ("notes", f"{_count(M.Note, user)} recorded", "records:notes_list", "View"),
        (
            "memory",
            f"{_count(M.MemoryEntry, user, status='active')} active"
            f" · {_count(M.MemoryEntry, user, kind='follow_up', status='active')} follow-up"
            f"{'s' if _count(M.MemoryEntry, user, kind='follow_up', status='active') != 1 else ''}",
            "records:memory_list",
            "Open",
        ),
    ]
    return [
        {
            "domain": domain,
            "label": DOMAIN_LABELS.get(domain, domain.capitalize()),
            "state": M.domain_state(user, domain),
            "summary": summary,
            "url": reverse(url_name),
            "action": action,
        }
        for domain, summary, url_name, action in lines
    ]
