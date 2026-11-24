"""UI smoke: the survival surfaces, the segmented state control, no echoed values."""

import re
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from records import models as M


def at(day="2026-11-02", hour=8, minute=0):
    return f"{day}T{hour:02d}:{minute:02d}"


def test_dashboard_starts_every_domain_unknown(logged_in):
    response = logged_in.get(reverse("records:dashboard"))
    assert response.status_code == 200
    states = {line["domain"]: line["state"] for line in response.context["lines"]}
    assert states["conditions"] == "unknown"
    assert states["measurements"] == "unknown"
    assert "Not asked yet" not in "".join(states.values())


def test_blood_pressure_creates_panel_two_components_and_a_pulse_row(user, logged_in):
    response = logged_in.post(
        reverse("records:quick_measurement"),
        {"systolic": 120, "diastolic": 80, "pulse": 60, "at": at(), "note": ""},
    )
    assert response.status_code == 303
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    assert panel.components.count() == 2
    assert panel.components.filter(metric="systolic_bp", value=120).exists()
    assert M.Measurement._default_manager.get(user=user, metric="pulse").parent is None
    assert M.Measurement._default_manager.filter(user=user).count() == 4


def test_second_dose_tap_is_a_no_op(user, logged_in, course):
    medication = course(started=date(2026, 11, 2))
    medication.save()
    slot = timezone.make_aware(datetime(2026, 11, 2, 8, 0)).isoformat()
    for _ in range(2):
        response = logged_in.post(
            reverse("records:quick_dose"),
            {"medication_id": medication.pk, "scheduled_for": slot, "status": "taken"},
        )
        assert response.status_code == 303
    assert M.DoseEvent._default_manager.filter(user=user).count() == 1


def test_symptom_severity_zero_is_stored(user, logged_in):
    response = logged_in.post(
        reverse("records:quick_symptom"),
        {"name": "Headache", "severity": "0", "side": "na", "onset_at": at()},
    )
    assert response.status_code == 303
    assert M.Symptom._default_manager.get(user=user).severity == 0


def test_checkin_stores_six_ratings(user, logged_in):
    data = {
        "at": at(),
        **{
            field: "3"
            for field in ("energy", "mood", "hunger", "soreness", "sleep_quality", "stress")
        },
    }
    response = logged_in.post(reverse("records:quick_checkin"), data)
    assert response.status_code == 303
    checkin = M.Checkin._default_manager.get(user=user)
    assert [
        getattr(checkin, field)
        for field in ("energy", "mood", "hunger", "soreness", "sleep_quality", "stress")
    ] == [3] * 6


def test_domain_state_control_distinguishes_unknown_from_none(user, logged_in):
    response = logged_in.post(reverse("records:state", args=["conditions"]), {"state": "none"})
    assert response.status_code == 303
    assert M.domain_state(user, "conditions") == "none"
    M.Condition._default_manager.create(user=user, name="Asthma")
    assert M.domain_state(user, "conditions") == "present"


def test_form_errors_never_echo_the_submitted_value(user, logged_in):
    sentinel = "SENTINEL-VALUE"
    response = logged_in.post(
        reverse("records:person"),
        {
            "name": "",
            "height_cm": sentinel,
            "timezone": "Europe/Stockholm",
            "unit_system": "metric",
            "birth_date_precision": "day",
            "sex_at_birth": "unknown",
        },
    )
    assert response.status_code == 200
    error_blocks = re.findall(
        r'<div class="errors" role="alert">(.*?)</div>', response.content.decode(), re.S
    )
    assert error_blocks
    assert sentinel not in "".join(error_blocks)


def test_person_form_saves_and_sets_the_account_timezone(user, logged_in):
    response = logged_in.post(
        reverse("records:person"),
        {
            "name": "Alice",
            "birth_date": "1985-04-02",
            "birth_date_precision": "day",
            "sex_at_birth": "female",
            "height_cm": "170.5",
            "timezone": "Europe/Stockholm",
            "unit_system": "metric",
            "locale": "en",
        },
    )
    assert response.status_code == 303
    person = M.Person._default_manager.get(user=user)
    assert person.name == "Alice"
    assert person.age_years > 30


def test_medication_course_change_splits_the_course_at_the_change_date(user, logged_in, course):
    medication = course(started=date(2026, 11, 1), strength=Decimal("8"))
    medication.full_clean()
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        {
            "kind": "medication",
            "substance": "Candesartan",
            "strength": "16",
            "unit": "mg",
            "route": "oral",
            "schedule_type": "daily",
            "times": "08:00",
            "started": "2026-11-01",
            "change_date": "2026-11-04",
            "with_food": "",
        },
    )
    assert response.status_code == 303
    courses = M.Medication._default_manager.filter(user=user).order_by("started")
    assert [c.started for c in courses] == [date(2026, 11, 1), date(2026, 11, 4)]
    assert courses[0].stopped == date(2026, 11, 3)
    assert [c.strength for c in courses] == [8, 16]
    from records import schedule as S

    slots = S.expected_doses_for_user(user, date(2026, 11, 1), date(2026, 11, 7))
    assert len(slots) == 7
    assert len({slot["date"] for slot in slots}) == 7


def test_course_change_without_a_date_is_refused(user, logged_in, course):
    medication = course(started=date(2026, 11, 1), strength=Decimal("8"))
    medication.full_clean()
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        {
            "kind": "medication",
            "substance": "Candesartan",
            "strength": "16",
            "unit": "mg",
            "route": "oral",
            "schedule_type": "daily",
            "times": "08:00",
            "started": "2026-11-01",
            "with_food": "",
        },
    )
    assert response.status_code == 200
    assert M.Medication._default_manager.filter(user=user).count() == 1


def test_timeline_and_regimen_render(user, logged_in, course):
    medication = course(started=date(2026, 11, 2))
    medication.save()
    assert logged_in.get(reverse("records:timeline")).status_code == 200
    response = logged_in.get(reverse("records:regimen"))
    assert response.status_code == 200
    assert "Candesartan" in response.content.decode()


def test_list_shows_a_badge_for_client_written_rows(user, logged_in):
    M.Allergy._default_manager.create(
        user=user, substance="Pollen", source="client", reported_by="health-mcp"
    )
    response = logged_in.get(reverse("records:allergies_list"))
    assert "health-mcp" in response.content.decode()


@pytest.mark.parametrize(
    "name",
    [
        "records:dashboard",
        "records:quick",
        "records:quick_dose",
        "records:quick_measurement",
        "records:quick_symptom",
        "records:quick_note",
        "records:quick_checkin",
        "records:quick_period",
        "records:timeline",
        "records:regimen",
        "records:person",
        "records:environment",
        "records:program",
        "records:goals_list",
        "records:quick_dose",
    ],
)
def test_screens_render(name, logged_in):
    assert logged_in.get(reverse(name)).status_code == 200
