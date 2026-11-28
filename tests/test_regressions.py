"""Regression tests for findings that would otherwise repeat: delete, split, silent fields."""

import importlib
from datetime import date
from decimal import Decimal

import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from records import models as M
from records import schedule as S
from records import views as V


@pytest.mark.parametrize("slug", sorted(V.RESOURCES))
def test_the_owner_can_delete_their_own_row(slug, logged_in, make_row):
    obj = make_row(slug)
    response = logged_in.post(reverse(f"records:{slug}_delete", args=[obj.pk]))
    assert response.status_code == 303
    assert not V.RESOURCES[slug]["model"]._default_manager.filter(pk=obj.pk).exists()


def test_deleting_a_course_with_logged_doses_is_refused_cleanly(user, logged_in, course):
    medication = course(started=date(2026, 11, 2))
    medication.save()
    M.Dose._default_manager.create(
        user=user, medication=medication, status="taken", at="2026-11-02T08:00:00Z"
    )
    response = logged_in.post(reverse("records:medications_delete", args=[medication.pk]))
    assert response.status_code == 302
    assert M.Medication._default_manager.filter(pk=medication.pk).exists()


def medication_post(**overrides):
    data = {
        "kind": "medication",
        "substance": "Candesartan",
        "strength": "8",
        "unit": "mg",
        "route": "oral",
        "schedule_type": "daily",
        "times": "08:00",
        "started": "2026-11-01",
        "with_food": "",
    }
    data.update(overrides)
    return data


def test_a_schedule_only_change_splits_the_course(user, logged_in, course):
    medication = course(started=date(2026, 11, 1))
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        medication_post(times="20:00", change_date="2026-11-04"),
    )
    assert response.status_code == 303
    old, new = M.Medication._default_manager.filter(user=user).order_by("started")
    assert old.stopped == date(2026, 11, 3)
    assert old.schedule["times"] == ["08:00"]
    assert new.started == date(2026, 11, 4)
    assert new.schedule["times"] == ["20:00"]
    slots = S.expected_doses_for_user(user, date(2026, 11, 1), date(2026, 11, 7))
    assert len({slot["date"] for slot in slots}) == 7


def test_a_course_can_be_stopped_from_the_form(user, logged_in, course):
    medication = course(started=date(2026, 11, 1), strength=Decimal("8"))
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        medication_post(stopped="2026-11-20"),
    )
    assert response.status_code == 303
    assert M.Medication._default_manager.get(pk=medication.pk).stopped == date(2026, 11, 20)


def test_stopping_before_starting_is_a_field_error(user, logged_in, course):
    medication = course(started=date(2026, 11, 1), strength=Decimal("8"))
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        medication_post(stopped="2026-10-01"),
    )
    assert response.status_code == 200
    assert M.Medication._default_manager.get(pk=medication.pk).stopped is None


def test_editing_an_earlier_stopped_course_is_allowed(user, logged_in, course):
    old = course(started=date(2026, 1, 1), stopped=date(2026, 1, 31))
    old.save()
    new = course(started=date(2026, 3, 1))
    new.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[old.pk]),
        medication_post(started="2026-01-01", stopped="2026-01-31", purpose_note="checked"),
    )
    assert response.status_code == 303
    assert M.Medication._default_manager.get(pk=old.pk).stopped == date(2026, 1, 31)


def test_a_malformed_schedule_type_is_a_field_error(user, logged_in, course):
    medication = course(started=date(2026, 11, 1), strength=Decimal("8"))
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        medication_post(schedule_type="hourly", change_date="2026-11-04"),
    )
    assert response.status_code == 200


def test_note_tags_are_persisted(user, logged_in):
    response = logged_in.post(
        reverse("records:notes_new"),
        {"at": "2026-11-02T08:00", "body": "Felt fine", "tags": "training, sleep"},
    )
    assert response.status_code == 303
    assert M.Note._default_manager.get(user=user).tags == ["training", "sleep"]


def test_injury_limits_are_persisted(user, logged_in):
    response = logged_in.post(
        reverse("records:injuries_new"),
        {
            "area": "knee",
            "side": "left",
            "since_precision": "day",
            "status": "active",
            "verification_status": "confirmed",
            "limits": "avoid squat\ncaution overhead press",
        },
    )
    assert response.status_code == 303
    assert M.Injury._default_manager.get(user=user).limits == [
        {"pattern": "squat", "kind": "avoid"},
        {"pattern": "overhead", "kind": "caution", "exercise": "press"},
    ]


def test_program_workout_types_are_persisted(user, logged_in):
    response = logged_in.post(
        reverse("records:program"),
        {"name": "Base", "workout_types": "strength_training, Running!"},
    )
    assert response.status_code == 303
    assert M.Program._default_manager.get(user=user).workout_types == [
        "strength_training",
        "running",
    ]


def test_a_non_numeric_measurement_is_a_field_error_not_a_500(user, logged_in):
    response = logged_in.post(
        reverse("records:measurements_new"),
        {"metric": "body_weight", "value": "abc", "at": "2026-11-02T08:00"},
    )
    assert response.status_code == 200
    assert not M.Measurement._default_manager.filter(user=user).exists()


def test_an_outlier_is_stored_with_a_warning(user, logged_in):
    response = logged_in.post(
        reverse("records:measurements_new"),
        {"metric": "body_weight", "value": "450", "at": "2026-11-02T08:00"},
        follow=True,
    )
    assert response.status_code == 200
    assert M.Measurement._default_manager.get(user=user).value == Decimal("450")
    assert "usual range" in response.content.decode()


def test_blood_pressure_components_are_not_listed_alone(user, logged_in):
    logged_in.post(
        reverse("records:measurements_new"),
        {
            "metric": "blood_pressure",
            "systolic": 120,
            "diastolic": 80,
            "at": "2026-11-02T08:00",
        },
    )
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    component = panel.components.first()
    listing = logged_in.get(reverse("records:measurements_list"))
    assert len(listing.context["rows"]) == 1  # the panel only; its numbers are components
    component_url = reverse("records:measurements_detail", args=[component.pk])
    assert logged_in.get(component_url).status_code == 404
    detail = logged_in.get(reverse("records:measurements_detail", args=[panel.pk]))
    assert "120" in detail.content.decode()


def test_medication_detail_shows_the_schedule_in_words(user, logged_in, course):
    medication = course(started=date(2026, 11, 2))
    medication.save()
    detail_url = reverse("records:medications_detail", args=[medication.pk])
    body = logged_in.get(detail_url).content.decode()
    assert "daily at 08:00" in body
    assert "{&#x27;type&#x27;" not in body


# --- Second review round ---------------------------------------------------------------


def test_a_new_medication_course_can_be_created_through_the_form(user, logged_in):
    response = logged_in.post(reverse("records:medications_new"), medication_post())
    assert response.status_code == 303
    course = M.Medication._default_manager.get(user=user)
    assert course.substance == "Candesartan"
    assert course.user == user


def test_checkin_renders_a_radio_group_per_rating(logged_in):
    body = logged_in.get(reverse("records:quick_checkin")).content.decode()
    for field in ("energy", "mood", "hunger", "soreness", "sleep_quality", "stress"):
        assert body.count(f'name="{field}"') == 5
    assert body.count('type="radio"') == 30


def test_a_symptom_without_severity_is_saved(logged_in, user):
    response = logged_in.post(
        reverse("records:quick_symptom"),
        {"name": "Headache", "severity": "", "side": "na", "onset_at": "2026-11-02T08:00"},
    )
    assert response.status_code == 303
    assert M.Symptom._default_manager.get(user=user).severity is None


def test_a_course_change_keeps_the_submitted_stop_date(user, logged_in, course):
    medication = course(started=date(2026, 11, 1))
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        medication_post(change_date="2026-11-04", stopped="2026-11-10"),
    )
    assert response.status_code == 303
    old, new = M.Medication._default_manager.filter(user=user).order_by("started")
    assert old.stopped == date(2026, 11, 3)
    assert new.started == date(2026, 11, 4)
    assert new.stopped == date(2026, 11, 10)


def test_a_stop_date_before_the_change_date_is_a_field_error(user, logged_in, course):
    medication = course(started=date(2026, 11, 1))
    medication.save()
    response = logged_in.post(
        reverse("records:medications_edit", args=[medication.pk]),
        medication_post(change_date="2026-11-04", stopped="2026-11-02"),
    )
    assert response.status_code == 200
    assert M.Medication._default_manager.filter(user=user).count() == 1


def test_a_panel_edit_replaces_its_components_and_keeps_the_panel(user, logged_in):
    logged_in.post(
        reverse("records:measurements_new"),
        {
            "metric": "blood_pressure",
            "systolic": 120,
            "diastolic": 80,
            "at": "2026-11-02T08:00",
        },
    )
    panel = M.Measurement._default_manager.get(user=user, metric="blood_pressure")
    edit_url = reverse("records:measurements_edit", args=[panel.pk])
    form_page = logged_in.get(edit_url).content.decode()
    assert 'value="120"' in form_page and 'value="80"' in form_page
    response = logged_in.post(
        edit_url,
        {"metric": "blood_pressure", "systolic": 118, "diastolic": 78, "at": "2026-11-02T08:00"},
    )
    assert response.status_code == 303
    assert panel.components.get(metric="systolic_bp").value == 118
    assert panel.components.get(metric="diastolic_bp").value == 78
    assert M.Measurement._default_manager.filter(user=user).count() == 3  # panel + two numbers
    delete_url = reverse("records:measurements_delete", args=[panel.pk])
    assert logged_in.post(delete_url).status_code == 303
    assert not M.Measurement._default_manager.filter(user=user).exists()


def test_prn_courses_have_a_log_button(user, logged_in, course):
    prn = course(schedule={"type": "prn", "max_per_day": 2})
    prn.save()
    body = logged_in.get(reverse("records:quick_dose")).content.decode()
    assert "Candesartan" in body
    for _ in range(2):
        response = logged_in.post(
            reverse("records:quick_dose"), {"medication_id": prn.pk, "status": "taken"}
        )
        assert response.status_code == 303
    assert M.Dose._default_manager.filter(user=user).count() == 2


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


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_a_dose_logged_at_a_dst_boundary_shows_as_logged(user, saved_course, day):
    medication = saved_course(
        schedule={"type": "daily", "times": ["02:30"]}, started=date(2026, 1, 1)
    )
    slot = S.planned_slots(user, day, day)[0]["scheduled_for"]
    M.Dose._default_manager.create(
        user=user, medication=medication, status="taken", at=slot, scheduled_for=slot
    )
    assert S.planned_slots(user, day, day)[0]["status"] == "taken"


def test_a_retired_permission_hands_its_grants_to_the_replacement(user):
    """Dropping the old permission row must not drop what was granted through it.

    An installation that ran 0006 on its own can hold add_period beside add_event, with a user
    or a group granted the retired one; deleting the row alone takes those grants with it.
    """
    migration = importlib.import_module(
        "records.migrations.0007_dose_related_name_and_retired_permissions"
    )
    content_type = ContentType.objects.get_for_model(M.Event)
    retired = Permission.objects.create(
        content_type=content_type, codename="add_period", name="Can add period"
    )
    user.user_permissions.add(retired)
    group = Group.objects.create(name="household")
    group.permissions.add(retired)

    migration.rename_permissions(apps, {"period": "event"}, "default")

    assert not Permission.objects.filter(pk=retired.pk).exists()
    assert user.user_permissions.filter(codename="add_event").exists()
    assert group.permissions.filter(codename="add_event").exists()
