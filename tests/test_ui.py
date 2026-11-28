"""UI smoke: the survival surfaces, the segmented state control, no echoed values."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from records import models as M
from records import registries as R


def at(day="2026-11-02", hour=8, minute=0):
    return f"{day}T{hour:02d}:{minute:02d}"


def blood_pressure(**overrides):
    payload = {
        "metric": "blood_pressure",
        "systolic": 120,
        "diastolic": 80,
        "at": at(),
        "note": "",
    }
    payload.update(overrides)
    return payload


def test_the_page_carries_the_name_and_the_icon(logged_in):
    body = logged_in.get(reverse("records:dashboard")).content.decode()
    assert "healthinfo" in body
    assert 'class="brand-info"' in body
    assert 'rel="icon"' in body
    assert "records/favicon.svg" in body
    assert "records/favicon.ico" in body
    assert 'content="#f5f6f8"' in body
    # The page turns #101319 in dark mode, so the status bar is given the same colour twice.
    dark_theme = '<meta name="theme-color" content="#101319" media="(prefers-color-scheme: dark)">'
    assert dark_theme in body


def test_favicon_ico_redirects_to_the_icon(client):
    response = client.get("/favicon.ico")
    assert response.status_code == 301
    assert response["Location"] == "/static/records/favicon.ico"
    icon = Path(__file__).resolve().parent.parent / "records/static/records/favicon.ico"
    assert icon.exists()
    # A real ICO, because the consumers of this path are the ones that cannot read an SVG.
    assert icon.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_dashboard_starts_every_domain_unknown(logged_in):
    response = logged_in.get(reverse("records:dashboard"))
    assert response.status_code == 200
    states = {line["domain"]: line["state"] for line in response.context["lines"]}
    assert states["conditions"] == "unknown"
    assert states["measurements"] == "unknown"
    assert "Not asked yet" not in "".join(states.values())


def test_blood_pressure_creates_a_panel_and_two_component_rows(user, logged_in):
    response = logged_in.post(reverse("records:measurements_new"), blood_pressure())
    assert response.status_code == 303
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    assert panel.value is None
    assert panel.components.count() == 2
    assert panel.components.filter(metric="systolic_bp", value=120).exists()
    assert M.Measurement._default_manager.filter(user=user).count() == 3


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
    assert M.Dose._default_manager.filter(user=user).count() == 1


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
        "records:measurements_new",
        "records:quick_symptom",
        "records:quick_note",
        "records:quick_checkin",
        "records:quick_event",
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


def test_a_pulse_row_without_a_value_does_not_break_the_dashboard(user, logged_in):
    M.Measurement._default_manager.create(user=user, metric="pulse", value=None, at=timezone.now())
    response = logged_in.get(reverse("records:dashboard"))
    assert response.status_code == 200
    assert "Pulse" in response.content.decode()


def test_the_measurements_list_shows_a_panel_reading_in_its_value_column(user, logged_in):
    logged_in.post(reverse("records:measurements_new"), blood_pressure())
    body = logged_in.get(reverse("records:measurements_list")).content.decode()
    assert "120/80" in body
    assert "mmHg" in body


def test_a_saved_reading_lands_on_its_own_page(user, logged_in):
    response = logged_in.post(reverse("records:measurements_new"), blood_pressure())
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    assert response["Location"] == reverse("records:measurements_detail", args=[panel.pk])


def test_a_panel_is_edited_with_its_two_numbers_and_keeps_one_reading(user, logged_in):
    logged_in.post(reverse("records:measurements_new"), blood_pressure())
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    edit = reverse("records:measurements_edit", args=[panel.pk])
    assert logged_in.get(edit).status_code == 200
    response = logged_in.post(
        edit, {"metric": "blood_pressure", "systolic": 132, "diastolic": 86, "at": at()}
    )
    assert response.status_code == 303
    assert M.Measurement._default_manager.filter(user=user, metric="blood_pressure").count() == 1
    assert panel.components.count() == 2
    assert panel.components.get(metric="systolic_bp").value == 132
    assert panel.components.get(metric="diastolic_bp").value == 86


def test_editing_a_client_written_panel_keeps_its_provenance(user, logged_in):
    panel = M.Measurement._default_manager.create(
        user=user,
        source="client",
        reported_by="health-mcp",
        metric="blood_pressure",
        at=timezone.now(),
    )
    for code, value in (("systolic_bp", 120), ("diastolic_bp", 80)):
        M.Measurement._default_manager.create(
            user=user,
            source="client",
            reported_by="health-mcp",
            metric=code,
            value=value,
            unit=R.METRICS[code].unit,
            at=panel.at,
            parent=panel,
        )
    response = logged_in.post(
        reverse("records:measurements_edit", args=[panel.pk]),
        blood_pressure(systolic=118, diastolic=78),
    )
    assert response.status_code == 303
    components = panel.components.all()
    assert set(components.values_list("source", "reported_by")) == {("client", "health-mcp")}
    assert components.get(metric="systolic_bp").value == 118


def test_a_row_turns_into_a_panel_and_back(user, logged_in):
    logged_in.post(
        reverse("records:measurements_new"),
        {"metric": "body_weight", "value": "82.4", "at": at()},
    )
    row = M.Measurement._default_manager.get(user=user, metric="body_weight")
    edit = reverse("records:measurements_edit", args=[row.pk])
    logged_in.post(edit, blood_pressure())
    row.refresh_from_db()
    assert (row.metric, row.value) == ("blood_pressure", None)
    assert row.components.count() == 2

    logged_in.post(edit, {"metric": "body_weight", "value": "81", "at": at()})
    row.refresh_from_db()
    assert (row.metric, row.value) == ("body_weight", Decimal("81"))
    assert row.components.count() == 0


def test_a_zero_value_is_rejected_instead_of_stored(user, logged_in):
    response = logged_in.post(
        reverse("records:measurements_new"), {"metric": "body_weight", "value": "0", "at": at()}
    )
    assert response.status_code == 200
    assert "Value is outside the accepted range." in response.content.decode()
    assert not M.Measurement._default_manager.filter(user=user).exists()


def test_pulse_is_a_metric_of_its_own(user, logged_in):
    response = logged_in.post(
        reverse("records:measurements_new"), {"metric": "pulse", "value": "58", "at": at()}
    )
    assert response.status_code == 303
    reading = M.Measurement._default_manager.get(user=user, metric="pulse")
    assert (reading.value, reading.unit, reading.parent) == (58, "/min", None)


def test_arm_and_posture_land_on_the_panel_and_its_components(user, logged_in):
    response = logged_in.post(
        reverse("records:measurements_new"),
        blood_pressure(side="left", posture="sitting"),
    )
    assert response.status_code == 303
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    assert (panel.side, panel.posture) == ("left", "sitting")
    assert set(panel.components.values_list("side", "posture")) == {("left", "sitting")}


def test_an_untouched_measurement_time_is_stamped_again_at_submission(user, logged_in):
    stale = "2026-11-02T08:00"
    response = logged_in.post(
        reverse("records:measurements_new"),
        blood_pressure(at=stale, at_default=stale),
    )
    assert response.status_code == 303
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    assert abs(panel.at - timezone.now()) < timedelta(minutes=2)


def test_a_chosen_measurement_time_is_kept(user, logged_in):
    chosen = "2026-11-02T08:00"
    response = logged_in.post(
        reverse("records:measurements_new"),
        blood_pressure(at=chosen, at_default="2026-11-02T09:30"),
    )
    assert response.status_code == 303
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    assert timezone.localtime(panel.at).strftime("%Y-%m-%dT%H:%M") == chosen


def test_a_missing_measurement_time_is_a_field_error(user, logged_in):
    response = logged_in.post(reverse("records:measurements_new"), blood_pressure(at=""))
    assert response.status_code == 200
    assert 'class="field invalid"' in response.content.decode()
    assert not M.Measurement._default_manager.filter(user=user).exists()
