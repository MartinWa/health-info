"""Registry constants: metrics, enums and validators shared by forms, views and importer.

Codes are append-only. A stored code that no longer resolves is a bug, and
`tests/test_registry.py` proves every value used here still resolves. Anything not listed
is free text with a length cap, never an enum.
"""

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

REGISTRY_VERSION = 1


def choices(values):
    """(code, label) pairs, so one tuple feeds models, forms and templates."""
    return [(value, value.replace("_", " ").capitalize()) for value in values]


def _check_member(value, allowed, label):
    if value not in allowed:
        raise ValidationError(f"Unknown {label}.")
    return value


# --- Enums (Appendix A.9) -------------------------------------------------------------
SEX_AT_BIRTH = ("female", "male", "other", "unknown")
UNIT_SYSTEM = ("metric", "imperial")
URBAN_RURAL = ("urban", "suburban", "rural", "unknown")
GYM_ACCESS = ("none", "home", "limited", "full")
WORK_PATTERN = ("day", "shift", "remote", "mixed", "none", "unknown")
WORK_LOAD = ("light", "moderate", "heavy", "unknown")
TOBACCO = ("never", "former", "current")
ALCOHOL = ("never", "occasional", "weekly", "daily")
COOKING_FACILITIES = ("full", "basic", "none")
PERIOD_KIND = ("travel", "illness", "work_crunch", "break", "other")
DATE_PRECISION = ("year", "month", "day")
GOAL_DIRECTION = ("decrease", "increase", "maintain")
GOAL_STATUS = ("active", "achieved", "abandoned", "paused")
CONDITION_STATUS = ("active", "managed", "resolved", "in_remission")
VERIFICATION_STATUS = ("unconfirmed", "suspected", "confirmed", "refuted")
SEVERITY = ("mild", "moderate", "severe")
INJURY_STATUS = ("active", "recovering", "resolved")
LIMIT_KIND = ("avoid", "caution")
ALLERGY_TYPE = ("allergy", "intolerance")
ALLERGY_SEVERITY = ("mild", "moderate", "severe", "anaphylaxis")
ALLERGY_CRITICALITY = ("low", "high", "unable_to_assess")
MEDICATION_KIND = ("medication", "supplement")
SCHEDULE_TYPE = ("daily", "interval", "weekly", "cycle", "prn")
WITH_FOOD = ("no_requirement", "with", "without")
DOSE_STATUS = ("taken", "skipped")
SYMPTOM_INTERFERENCE = ("none", "mild", "moderate", "severe")
POSTURE = ("sitting", "standing", "lying")
SIDE = ("left", "right")
BODY_SIDE = ("left", "right", "both", "na")
VACCINATION_STATUS = ("completed", "declined", "entered_in_error")
APPOINTMENT_STATUS = ("planned", "attended", "missed", "cancelled")
SOURCE = ("owner", "client", "import", "device")
DOMAIN_STATE = ("unknown", "none", "present")
MEMORY_KIND = (
    "fact",
    "preference",
    "observation",
    "episode",
    "goal_progress",
    "correction",
    "follow_up",
)
MEMORY_STATUS = ("active", "superseded", "done")

DOMAINS = (
    "identity",
    "environment",
    "goals",
    "program",
    "conditions",
    "injuries",
    "allergies",
    "medications",
    "measurements",
    "symptoms",
    "checkins",
    "labs",
    "vaccinations",
    "appointments",
    "notes",
    "memory",
)

ROUTES = (
    "oral",
    "sublingual",
    "subcutaneous",
    "intramuscular",
    "intravenous",
    "topical",
    "transdermal",
    "inhaled",
    "nasal",
    "ophthalmic",
    "rectal",
    "vaginal",
    "other",
)

# --- A.3 Equipment ---------------------------------------------------------------------
EQUIPMENT = (
    "barbell",
    "rack",
    "bench",
    "dumbbells",
    "kettlebells",
    "machines",
    "cables",
    "pullup_bar",
    "bands",
    "bodyweight",
    "cardio_machine",
    "rower",
    "cycle",
    "treadmill",
    "pool",
    "sled",
    "medicine_ball",
    "rings",
)

# --- A.4 Workout types: open list, never rejected --------------------------------------
WORKOUT_TYPES = (
    "strength_training",
    "powerlifting",
    "bodybuilding",
    "crossfit",
    "hiit",
    "running",
    "walking",
    "hiking",
    "cycling",
    "swimming",
    "rowing",
    "elliptical",
    "stair_climbing",
    "yoga",
    "pilates",
    "mobility",
    "stretching",
    "climbing",
    "skiing",
    "tennis",
    "football",
    "basketball",
    "martial_arts",
    "golf",
    "dancing",
    "other",
)

# --- A.5 Movement patterns -------------------------------------------------------------
MOVEMENT_PATTERNS = (
    "squat",
    "hinge",
    "lunge",
    "push",
    "pull",
    "overhead",
    "carry",
    "knee_flexion",
    "spinal_load",
    "grip",
    "rotation",
    "impact",
    "conditioning",
)

# --- A.6 Body areas --------------------------------------------------------------------
BODY_AREAS = (
    "head",
    "neck",
    "shoulder",
    "elbow",
    "wrist",
    "hand",
    "chest",
    "ribs",
    "mid_back",
    "low_back",
    "abdomen",
    "hip",
    "groin",
    "quad",
    "hamstring",
    "knee",
    "shin",
    "calf",
    "ankle",
    "foot",
    "skin",
    "other",
)

# --- A.7 Units -------------------------------------------------------------------------
STRENGTH_UNITS = (
    "ug",
    "mg",
    "g",
    "mL",
    "[IU]",
    "{tablet}",
    "{dose}",
    "{puff}",
    "{spray}",
    "{patch}",
)
UNIT_DISPLAY = {
    "ug": "µg",
    "mg": "mg",
    "g": "g",
    "mL": "ml",
    "mm[Hg]": "mmHg",
    "[IU]": "IU",
    "{tablet}": "tablet",
    "{dose}": "dose",
    "{puff}": "puff",
    "{spray}": "spray",
    "{patch}": "patch",
}
UNIT_ALIASES = {
    "µg": "ug",
    "mcg": "ug",
    "ug": "ug",
    "mg": "mg",
    "g": "g",
    "ml": "mL",
    "mL": "mL",
    "iu": "[IU]",
    "[iu]": "[IU]",
    "[IU]": "[IU]",
    "tablet": "{tablet}",
    "dose": "{dose}",
    "puff": "{puff}",
    "spray": "{spray}",
    "patch": "{patch}",
}

# --- A.10 Check-in anchors -------------------------------------------------------------
CHECKIN_FIELDS = ("energy", "mood", "hunger", "soreness", "sleep_quality", "stress")
CHECKIN_ANCHORS = {
    "energy": ("drained", "plenty"),
    "mood": ("low", "good"),
    "hunger": ("none", "ravenous"),
    "soreness": ("none", "severe"),
    "sleep_quality": ("bad", "good"),
    "stress": ("calm", "high"),
}
SYMPTOM_SEVERITY_MAX = 10


def canonical_unit(value):
    """Accept the printed form and return the canonical UCUM spelling."""
    text = (value or "").strip()
    if text in UNIT_ALIASES:
        return UNIT_ALIASES[text]
    if text in UNIT_DISPLAY:
        return text
    raise ValidationError("Unknown unit.")


@dataclass(frozen=True)
class Metric:
    code: str
    display: str
    unit: str
    digits: int
    plausible: tuple | None
    hard: tuple | None
    use: frozenset
    authority: str
    paired: str | None = None
    contexts: tuple = ()


def _metric(code, unit, digits, plausible, hard, use, authority, paired=None, contexts=()):
    return Metric(
        code=code,
        display=code.replace("_", " ").capitalize(),
        unit=unit,
        digits=digits,
        plausible=plausible,
        hard=hard,
        use=frozenset(use),
        authority=authority,
        paired=paired,
        contexts=tuple(contexts),
    )


METRICS = {
    metric.code: metric
    for metric in (
        _metric(
            "body_weight",
            "kg",
            1,
            (30, 400),
            (1, 500),
            ("log", "goal"),
            "external:open_wearables",
            contexts=("fasted",),
        ),
        _metric(
            "waist_cm",
            "cm",
            1,
            (40, 250),
            (10, 300),
            ("log", "goal"),
            "healthinfo",
            contexts=("fasted",),
        ),
        _metric("blood_pressure", "", 0, None, None, ("panel",), "healthinfo"),
        _metric(
            "systolic_bp",
            "mm[Hg]",
            0,
            (50, 260),
            (20, 400),
            ("log",),
            "healthinfo",
            paired="diastolic_bp",
            contexts=("posture", "side"),
        ),
        _metric(
            "diastolic_bp",
            "mm[Hg]",
            0,
            (20, 160),
            (5, 250),
            ("log",),
            "healthinfo",
            paired="systolic_bp",
            contexts=("posture", "side"),
        ),
        _metric("pulse", "/min", 0, (25, 250), (10, 350), ("log",), "healthinfo"),
        _metric(
            "body_fat_pct", "%", 1, (2, 70), (0.5, 85), ("log", "goal"), "external:open_wearables"
        ),
        _metric(
            "workout_sessions",
            "{session}",
            0,
            None,
            (0, 10000),
            ("goal",),
            "external:open_wearables",
        ),
    )
}

METRIC_ALIASES = {
    "bodyweight": "body_weight",
    "weight": "body_weight",
    "body_weight_kg": "body_weight",
    "waist": "waist_cm",
    "waist_cm": "waist_cm",
    "bp": "blood_pressure",
    "bloodpressure": "blood_pressure",
    "blood_pressure": "blood_pressure",
    "sys": "systolic_bp",
    "systolic": "systolic_bp",
    "dia": "diastolic_bp",
    "diastolic": "diastolic_bp",
    "hr": "pulse",
    "heart_rate": "pulse",
    "pulse": "pulse",
    "body_fat": "body_fat_pct",
    "bf": "body_fat_pct",
    "workouts": "workout_sessions",
    "sessions": "workout_sessions",
    "workout": "workout_sessions",
}

METRIC_CONTEXT_VALUES = {
    "posture": POSTURE,
    "side": SIDE,
    "fasted": (True, False),
}


def resolve_metric(value):
    """Return the canonical Metric for an input code or alias."""
    code = METRIC_ALIASES.get((value or "").strip().lower(), (value or "").strip().lower())
    metric = METRICS.get(code)
    if metric is None:
        raise ValidationError("Unknown metric.")
    return metric


def check_value(metric, value):
    """Warnings for an implausible reading; a unit or sign error is rejected."""
    if value is None:
        return []
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("Value must be a number.") from exc
    if not number.is_finite() or number != number or math.isinf(float(number)):
        raise ValidationError("Value must be a finite number.")
    if metric.hard and not metric.hard[0] <= float(number) <= metric.hard[1]:
        raise ValidationError("Value is outside the accepted range.")
    if metric.plausible and not metric.plausible[0] <= float(number) <= metric.plausible[1]:
        return [f"{metric.display} is outside the usual range; stored as entered."]
    return []


_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate_schedule(data):
    """Normalise one of the five schedule shapes; raise ValidationError when inconsistent."""
    if not isinstance(data, dict):
        raise ValidationError("Schedule must be an object.")
    kind = data.get("type")
    _check_member(kind, SCHEDULE_TYPE, "schedule type")
    schedule = {"type": kind}
    if kind == "interval":
        every = data.get("every_days")
        if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= 365:
            raise ValidationError("An interval schedule needs every_days between 1 and 365.")
        schedule["every_days"] = every
    elif kind == "weekly":
        days = data.get("days_of_week")
        if not isinstance(days, list) or not days:
            raise ValidationError("A weekly schedule needs at least one weekday.")
        if any(
            not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 7 for day in days
        ):
            raise ValidationError("Weekdays are ISO numbers 1 to 7.")
        schedule["days_of_week"] = sorted(set(days))
    elif kind == "cycle":
        on_days = data.get("on_days")
        off_days = data.get("off_days")
        if not isinstance(on_days, int) or isinstance(on_days, bool) or not 1 <= on_days <= 365:
            raise ValidationError("A cycle schedule needs on_days between 1 and 365.")
        if not isinstance(off_days, int) or isinstance(off_days, bool) or not 0 <= off_days <= 365:
            raise ValidationError("A cycle schedule needs off_days between 0 and 365.")
        schedule["on_days"] = on_days
        schedule["off_days"] = off_days
    elif kind == "prn":
        limit = data.get("max_per_day")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 24:
            raise ValidationError("A PRN schedule needs max_per_day between 1 and 24.")
        schedule["max_per_day"] = limit
    times = data.get("times")
    if times is not None:
        if not isinstance(times, list) or any(
            not isinstance(t, str) or not _TIME.match(t) for t in times
        ):
            raise ValidationError("Times are 24-hour HH:MM values.")
        schedule["times"] = sorted(set(times))
    if "with_food" in data:
        schedule["with_food"] = _check_member(data["with_food"], WITH_FOOD, "with_food value")
    return schedule


def normalise_workout_type(value):
    """Lowercase, underscores, no punctuation; unknown values are stored as-is."""
    # shortcut: punctuation is stripped, not rejected; upgrade when a device write path
    # accepts raw type strings.
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def normalise_subject(value):
    """One lowercase slug per memory subject so filters group together."""
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def area_label(area, side):
    """Display text for an area and side pair, e.g. 'left knee'."""
    label = (area or "").replace("_", " ")
    if side in ("left", "right"):
        return f"{side} {label}"
    return label
