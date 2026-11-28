"""Every registry code resolves, and each reject/warn row of Appendix A.11 has a case."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from records import forms as F
from records import models as M
from records import registries as R

ENUMS = [
    R.SEX_AT_BIRTH,
    R.UNIT_SYSTEM,
    R.URBAN_RURAL,
    R.GYM_ACCESS,
    R.WORK_PATTERN,
    R.WORK_LOAD,
    R.TOBACCO,
    R.ALCOHOL,
    R.COOKING_FACILITIES,
    R.PERIOD_KIND,
    R.DATE_PRECISION,
    R.GOAL_DIRECTION,
    R.GOAL_STATUS,
    R.CONDITION_STATUS,
    R.VERIFICATION_STATUS,
    R.SEVERITY,
    R.INJURY_STATUS,
    R.LIMIT_KIND,
    R.ALLERGY_TYPE,
    R.ALLERGY_SEVERITY,
    R.ALLERGY_CRITICALITY,
    R.MEDICATION_KIND,
    R.SCHEDULE_TYPE,
    R.WITH_FOOD,
    R.DOSE_STATUS,
    R.SYMPTOM_INTERFERENCE,
    R.POSTURE,
    R.SIDE,
    R.BODY_SIDE,
    R.VACCINATION_STATUS,
    R.APPOINTMENT_STATUS,
    R.SOURCE,
    R.DOMAIN_STATE,
    R.MEMORY_KIND,
    R.MEMORY_STATUS,
    R.ROUTES,
    R.STRENGTH_UNITS,
    R.EQUIPMENT,
    R.WORKOUT_TYPES,
    R.MOVEMENT_PATTERNS,
    R.BODY_AREAS,
]


def test_enum_members_are_unique_codes():
    for values in ENUMS:
        assert len(set(values)) == len(values)
        for value in values:
            assert value == value.strip(), value


def test_every_metric_resolves_and_its_unit_is_one_we_use():
    for code, metric in R.METRICS.items():
        assert R.resolve_metric(code) is metric
        assert metric.digits <= 4
        assert metric.unit in {"", "kg", "cm", "mm[Hg]", "/min", "%", "{session}"}
        if metric.paired:
            assert metric.paired in R.METRICS
            assert R.METRICS[metric.paired].paired == code
        if metric.plausible and metric.hard:
            assert metric.hard[0] <= metric.plausible[0] <= metric.plausible[1] <= metric.hard[1]


def test_aliases_resolve_to_canonical_codes():
    for alias, code in R.METRIC_ALIASES.items():
        assert R.resolve_metric(alias).code == code


def test_blood_pressure_panel_is_never_measurable_alone():
    assert "log" not in R.METRICS["blood_pressure"].use
    assert "panel" in R.METRICS["blood_pressure"].use
    assert "log" not in R.METRICS["workout_sessions"].use


def test_schedule_shapes_are_accepted():
    for schedule in (
        {"type": "daily", "times": ["08:00"]},
        {"type": "interval", "every_days": 14, "times": ["08:00"]},
        {"type": "weekly", "days_of_week": [1, 4]},
        {"type": "cycle", "on_days": 5, "off_days": 2},
        {"type": "prn", "max_per_day": 2},
    ):
        assert R.validate_schedule(schedule)["type"] == schedule["type"]


def test_schedule_normalises_times_and_weekdays():
    normalised = R.validate_schedule(
        {"type": "weekly", "days_of_week": [4, 1, 1], "times": ["20:00"]}
    )
    assert normalised["days_of_week"] == [1, 4]
    assert normalised["times"] == ["20:00"]


@pytest.mark.parametrize(
    "schedule",
    [
        {"type": "hourly"},
        {"type": "interval"},
        {"type": "weekly", "days_of_week": []},
        {"type": "weekly", "days_of_week": [8]},
        {"type": "cycle", "on_days": 5},
        {"type": "prn"},
        {"type": "daily", "times": ["25:00"]},
        {"type": "daily", "with_food": "sometimes"},
    ],
)
def test_inconsistent_or_unknown_schedules_are_rejected(schedule):
    with pytest.raises(ValidationError):
        R.validate_schedule(schedule)


def test_unknown_metric_unit_area_pattern_route_reject():
    with pytest.raises(ValidationError):
        R.resolve_metric("resting_hr")
    with pytest.raises(ValidationError):
        R.canonical_unit("furlong")
    with pytest.raises(ValidationError):
        R._check_member("toe", R.BODY_AREAS, "body area")
    with pytest.raises(ValidationError):
        R._check_member("twist", R.MOVEMENT_PATTERNS, "movement pattern")
    with pytest.raises(ValidationError):
        R._check_member("nasal_spray", R.ROUTES, "route")


def test_non_finite_and_sign_errors_reject():
    with pytest.raises(ValidationError):
        R.check_value(R.METRICS["body_weight"], Decimal("Infinity"))
    with pytest.raises(ValidationError):
        R.check_value(R.METRICS["body_weight"], Decimal("NaN"))
    with pytest.raises(ValidationError):
        R.check_value(R.METRICS["body_weight"], 0)
    with pytest.raises(ValidationError):
        R.check_value(R.METRICS["body_weight"], -5)


def test_an_outlier_warns_instead_of_rejecting():
    warnings = R.check_value(R.METRICS["body_weight"], 500)
    assert warnings and "usual range" in warnings[0]
    assert R.check_value(R.METRICS["body_weight"], 90) == []


def test_unit_mismatch_rejects_on_a_measurement(user):
    measurement = M.Measurement(
        user=user, metric="body_weight", value=90, unit="lb", at="2026-11-02T08:00:00Z"
    )
    with pytest.raises(ValidationError) as error:
        measurement.clean()
    assert "unit" in error.value.message_dict


def test_panel_without_both_components_rejects(user):
    form = F.MeasurementForm(
        {"metric": "blood_pressure", "systolic": 120, "at": "2026-11-02T08:00"}, user=user
    )
    assert not form.is_valid()


def test_end_before_start_rejects(user):
    form = F.PeriodForm({"kind": "travel", "start": "2026-11-05", "end": "2026-11-01"}, user=user)
    assert not form.is_valid()


def test_punctuation_in_a_workout_type_is_normalised_not_rejected():
    # shortcut: devices send raw strings; punctuation is folded rather than refused.
    assert R.normalise_workout_type("Strength Training!") == "strength_training"


def test_external_metric_goal_warns(user):
    form = F.GoalForm(
        {
            "metric": "body_weight",
            "direction": "decrease",
            "target": "88",
            "priority": 1,
            "status": "active",
        },
        user=user,
    )
    assert form.is_valid()
    assert any("outside healthinfo" in warning for warning in form.warnings)


def test_opposite_active_goals_warn(user):
    M.Goal._default_manager.create(user=user, metric="body_weight", direction="increase", target=95)
    form = F.GoalForm(
        {
            "metric": "body_weight",
            "direction": "decrease",
            "target": "88",
            "priority": 1,
            "status": "active",
        },
        user=user,
    )
    assert form.is_valid()
    assert any("opposite" in warning for warning in form.warnings)


def test_entered_in_error_vaccination_older_than_30_days_warns(user):
    form = F.VaccinationForm(
        {"vaccine": "Flu", "status": "entered_in_error", "at": "2026-01-01"}, user=user
    )
    assert form.is_valid()
    assert form.warnings
