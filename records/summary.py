"""Dashboard lines and the overview cards: today's doses, recent activity."""

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
    """Newest rows across the logged kinds, the same items the timeline lists.

    Each source is capped at `limit` in the database: the newest `limit` of one kind is
    the most it can contribute to the newest `limit` overall.
    """
    items = []

    def add(queryset, kind, text, url_name, when_field="at", order="-at"):
        for obj in queryset.order_by(order)[:limit]:
            items.append(
                {
                    "at": getattr(obj, when_field),
                    "kind": kind,
                    "text": text(obj),
                    "url": reverse(url_name, args=[obj.pk]) if url_name else None,
                }
            )

    add(
        M.Measurement._default_manager.filter(user=user, parent__isnull=True).prefetch_related(
            "components"
        ),
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
        "-onset_at",
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
        M.Dose._default_manager.filter(user=user).select_related("medication"),
        "Dose",
        lambda obj: f"{obj.medication} {obj.status}",
        None,
    )
    for event in M.Event._default_manager.filter(user=user).order_by("-start")[:limit]:
        items.append(
            {
                "at": timezone.make_aware(datetime.combine(event.start, datetime.min.time())),
                "kind": "Event",
                "text": f"{event.get_kind_display()} {event.start}",
                "url": reverse("records:events_detail", args=[event.pk]),
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]


def _title(code):
    return code.replace("_", " ").capitalize()


def _measurement_text(obj):
    """One reading, whatever shape it is stored in: a panel reads from its components."""
    return f"{_title(obj.metric)} {obj.reading} {obj.unit_text}".strip()


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
