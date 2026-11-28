"""The versioned JSON API: bearer tokens, owner-scoped rows, one envelope everywhere.

Every success carries `contract_version`, `generated_at` and `registry_version`;
collections add the page block beside `items`, singletons nest their payload under their
own key, and a write returns the resource bare. Errors never echo a value.
"""

import hashlib
import hmac
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.http import Http404, HttpRequest, HttpResponse
from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.errors import AuthenticationError, HttpError
from ninja.errors import ValidationError as NinjaValidationError
from ninja.security import HttpBearer
from pydantic import ConfigDict, Field, PlainSerializer, create_model

from records import models as M
from records import registries as R
from records import schedule as S

# A schema field named `date` would shadow the type inside the class body.
Date = date

CONTRACT_VERSION = "1.0"
MAX_RANGE_DAYS = 1830
DEFAULT_RANGE_DAYS = 30

# Pydantic v2 writes a Decimal as a JSON string; the contract says numbers, so responses
# use `Number`. Input schemas use plain `Decimal`: dumping a request through the float
# serialiser corrupts values like 88.1 before Django checks the scale.
Number = Annotated[Decimal, PlainSerializer(lambda value: float(value), return_type=float)]


class Problem(Exception):
    """An API failure: status, stable code, and a detail list that never holds a value."""

    def __init__(self, status: int, code: str, message: str, details: list | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


def invalid_field(path: str, message: str, code: str = "invalid") -> Problem:
    return Problem(422, code, message, [{"path": path, "message": message}])


# Codes whose Django message interpolates the submitted value.
SAFE_MESSAGES = {"invalid_choice": "Unknown value.", "invalid": "Invalid value."}


def _safe_error(error) -> str:
    if error.code in SAFE_MESSAGES:
        return SAFE_MESSAGES[error.code]
    return "; ".join(str(message) for message in error.messages)


def invalid(exc: DjangoValidationError) -> Problem:
    """Django's field errors become 422 details; the messages never carry a value."""
    if hasattr(exc, "error_dict"):
        details = [
            {"path": "" if field == "__all__" else field, "message": _safe_error(error)}
            for field, errors in exc.error_dict.items()
            for error in errors
        ]
    else:
        details = [{"path": "", "message": _safe_error(error)} for error in exc.error_list]
    return Problem(422, "invalid", "Some fields are invalid.", details)


class Bearer(HttpBearer):
    """A digest token: prefix lookup, then a constant-time compare of the sha256."""

    def authenticate(self, request: HttpRequest, token: str):
        row = M.ApiToken._default_manager.filter(prefix=token[:12]).first()
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = timezone.now()
        if (
            row is None
            or not hmac.compare_digest(row.key_hash, digest)
            or row.revoked_at is not None
            or (row.expires_at is not None and row.expires_at <= now)
            or not row.user.is_active
        ):
            # Bad, expired, revoked and disabled are indistinguishable to the caller.
            raise AuthenticationError(message="Unauthorized.")
        # A bearer request has no session, so the timezone middleware never activated.
        timezone.activate(S.account_timezone(row.user))
        M.ApiToken._default_manager.filter(pk=row.pk).update(last_used_at=now)
        return row


api = NinjaAPI(
    title="healthinfo",
    version=CONTRACT_VERSION,
    description="One household's authored health records. Bearer token, owner-scoped.",
    urls_namespace="api",
    auth=Bearer(),
)


def envelope() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": timezone.now(),
        "registry_version": R.REGISTRY_VERSION,
    }


def error_response(request: HttpRequest, status, code, message, details=None):
    return api.create_response(
        request,
        {"error": {"code": code, "message": message, "details": details or []}, **envelope()},
        status=status,
    )


@api.exception_handler(Problem)
def _problem(request, exc: Problem):
    return error_response(request, exc.status, exc.code, exc.message, exc.details)


@api.exception_handler(NinjaValidationError)
def _ninja_validation(request, exc: NinjaValidationError):
    details = []
    for error in exc.errors:
        # Ninja prefixes the source and the parameter name; the client needs the field.
        parts = [str(part) for part in error.get("loc", ())]
        while parts and parts[0] in ("body", "payload", "query", "path", "header"):
            parts.pop(0)
        details.append({"path": ".".join(parts), "message": str(error.get("msg", "Invalid."))})
    return error_response(request, 422, "invalid_request", "Some fields are invalid.", details)


@api.exception_handler(AuthenticationError)
def _unauthorized(request, exc: AuthenticationError):
    response = error_response(request, 401, "unauthorized", "Unauthorized.")
    response["WWW-Authenticate"] = 'Bearer realm="healthinfo"'
    return response


@api.exception_handler(Http404)
def _not_found(request, exc: Http404):
    return error_response(request, 404, "not_found", "Not found.")


@api.exception_handler(HttpError)
def _http_error(request, exc: HttpError):
    code = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(
        exc.status_code, "error"
    )
    return error_response(request, exc.status_code, code, str(exc))


@api.exception_handler(Exception)
def _unhandled(request, exc: Exception):
    # No traceback, no value and no path: the client only learns that it failed.
    return error_response(request, 500, "internal_error", "Internal error.")


# --- Output schemas -------------------------------------------------------------------


class RecordOut(Schema):
    id: int
    source: str
    reported_by: str
    created_at: datetime
    updated_at: datetime
    warnings: list[str] = Field(default_factory=list)


class Page[T](Schema):
    items: list[T]
    count: int
    limit: int
    offset: int
    has_more: bool
    contract_version: str
    generated_at: datetime
    registry_version: int


class LimitOut(Schema):
    pattern: str
    kind: str
    exercise: str | None = None
    note: str | None = None


class GoalOut(RecordOut):
    metric: str
    direction: str
    target: Number
    target_high: Number | None = None
    by: date | None = None
    review_at: date | None = None
    priority: int
    status: str
    baseline_value: Number | None = None
    baseline_on: date | None = None
    note: str


class ProgramOut(RecordOut):
    name: str
    phase: str
    started: date | None = None
    ended: date | None = None
    sessions_per_week: int | None = None
    deload_every_weeks: int | None = None
    progression: str
    workout_types: list[str]


class PeriodOut(RecordOut):
    kind: str
    start: date
    end: date | None = None
    note: str


class ConditionOut(RecordOut):
    name: str
    code: str
    since: date | None = None
    since_precision: str
    status: str
    verification_status: str
    severity: str
    abatement: str
    note: str
    practitioner_id: int | None = None


class InjuryOut(RecordOut):
    area: str
    side: str
    since: date | None = None
    since_precision: str
    status: str
    verification_status: str
    mechanism: str
    rehab: str
    limits: list[LimitOut]


class AllergyOut(RecordOut):
    substance: str
    type: str
    reaction: str
    severity: str
    criticality: str
    note: str


class MedicationOut(RecordOut):
    kind: str
    substance: str
    strength: Number | None = None
    unit: str
    route: str
    schedule: dict[str, Any]
    started: date | None = None
    stopped: date | None = None
    indication: str
    practitioner_id: int | None = None
    purpose_note: str


class DoseEventOut(RecordOut):
    medication_id: int
    status: str
    at: datetime
    scheduled_for: datetime | None = None
    dose: Number | None = None
    reason: str


class VaccinationOut(RecordOut):
    vaccine: str
    status: str
    dose_number: int | None = None
    lot: str
    at: date | None = None
    site: str


class LabOut(RecordOut):
    panel: str
    analyte: str
    value: Number | None = None
    value_text: str | None = None
    unit: str
    ref_low: Number | None = None
    ref_high: Number | None = None
    fasting: bool | None = None
    collected_at: datetime
    lab_name: str


class ComponentOut(Schema):
    id: int
    metric: str
    value: Number | None = None
    unit: str
    posture: str
    side: str
    fasted: bool | None = None


class MeasurementOut(RecordOut):
    metric: str
    value: Number | None = None
    unit: str
    at: datetime
    posture: str
    side: str
    fasted: bool | None = None
    device: str
    note: str
    components: list[ComponentOut] = Field(default_factory=list)


class SymptomOut(RecordOut):
    name: str
    severity: int | None = None
    interference: str
    onset_at: datetime
    resolved_at: datetime | None = None
    body_area: str
    side: str
    trigger: str
    note: str


class CheckinOut(RecordOut):
    at: datetime
    energy: int | None = None
    mood: int | None = None
    hunger: int | None = None
    soreness: int | None = None
    sleep_quality: int | None = None
    stress: int | None = None
    note: str


class NoteOut(RecordOut):
    at: datetime
    body: str
    tags: list[str]


class AppointmentOut(RecordOut):
    at: datetime
    practitioner_id: int | None = None
    practitioner_name: str
    specialty: str
    place: str
    reason: str
    status: str
    outcome_note: str


class MemoryOut(RecordOut):
    kind: str
    subject: str
    statement: str
    observed_at: datetime
    recorded_at: datetime
    source_ref: str
    importance: int
    status: str
    corrects_id: int | None = None
    valid_until: datetime | None = None
    pinned: bool
    locked: bool
    due_at: datetime | None = None
    tags: list[str]


class PractitionerOut(RecordOut):
    name: str
    specialty: str
    organisation: str
    contact: str


class PersonOut(Schema):
    name: str
    birth_date: date | None = None
    birth_date_precision: str
    sex_at_birth: str
    gender: str
    height_cm: Number | None = None
    timezone: str
    unit_system: str
    locale: str
    age_years: int | None = None


class PersonEnvelope(Schema):
    contract_version: str
    generated_at: datetime
    registry_version: int
    person: PersonOut


class EnvironmentOut(Schema):
    country: str
    region: str
    urban_rural: str
    gym_access: str
    equipment: list[str]
    work_pattern: str
    work_load: str
    cooks_for: int | None = None
    cooking_facilities: str
    tobacco: str
    tobacco_amount: str
    alcohol: str
    alcohol_units: str
    notes: str
    state: str = "unknown"


class EnvironmentEnvelope(Schema):
    contract_version: str
    generated_at: datetime
    registry_version: int
    environment: EnvironmentOut


class ExpectedDoseOut(Schema):
    medication_id: int
    substance: str
    strength: Number | None = None
    unit: str
    date: Date
    scheduled_for: datetime


class AdherenceOut(Schema):
    expected: int
    taken: int
    skipped: int
    unlogged: int
    coverage: float | None = None
    contract_version: str
    generated_at: datetime
    registry_version: int


class SummaryAllergy(Schema):
    substance: str
    type: str
    severity: str
    criticality: str


class SummaryLimit(Schema):
    pattern: str
    kind: str


class SummaryInjury(Schema):
    area: str
    side: str
    status: str
    verification_status: str
    limits: list[SummaryLimit]


class SummaryCondition(Schema):
    name: str
    status: str
    verification_status: str


class AllergyBlock(Schema):
    state: str
    items: list[SummaryAllergy]


class InjuryBlock(Schema):
    state: str
    items: list[SummaryInjury]


class ConditionBlock(Schema):
    state: str
    items: list[SummaryCondition]


class ConstraintsOut(Schema):
    allergies: AllergyBlock
    injuries: InjuryBlock
    conditions: ConditionBlock


class SummaryGoal(Schema):
    metric: str
    direction: str
    target: Number
    by: date | None = None
    priority: int


class SummaryProgram(Schema):
    name: str
    phase: str
    sessions_per_week: int | None = None


class SummaryPerson(Schema):
    age_years: int | None = None
    sex_at_birth: str
    height_cm: Number | None = None
    timezone: str


class SummaryEnvironment(Schema):
    gym_access: str
    work_pattern: str


class RegimenOut(Schema):
    active_medications: int
    indication_recorded: bool


class Availability(Schema):
    state: str
    count: int
    last_at: date | None = None


class AvailabilityOut(Schema):
    labs: Availability
    measurements: Availability


class SummaryMemory(Schema):
    pinned: int
    open_follow_ups: int


class SummaryOut(Schema):
    person: SummaryPerson
    goals: list[SummaryGoal]
    program: SummaryProgram | None = None
    environment: SummaryEnvironment
    constraints: ConstraintsOut
    regimen: RegimenOut
    availability: AvailabilityOut
    missing: list[str]
    memory: SummaryMemory


class SummaryEnvelope(Schema):
    contract_version: str
    generated_at: datetime
    registry_version: int
    summary: SummaryOut


class HealthOut(Schema):
    status: str
    version: str
    contract_version: str
    generated_at: datetime
    registry_version: int
    today: date
    counts: dict[str, int]


class Envelope(Schema):
    contract_version: str
    generated_at: datetime
    registry_version: int


def enveloped(schema):
    """An item or write response: the resource fields plus the envelope keys."""
    return create_model(f"{schema.__name__}Item", __base__=(schema, Envelope))


GoalItem = enveloped(GoalOut)
ProgramItem = enveloped(ProgramOut)
PeriodItem = enveloped(PeriodOut)
ConditionItem = enveloped(ConditionOut)
InjuryItem = enveloped(InjuryOut)
DoseEventItem = enveloped(DoseEventOut)
MeasurementItem = enveloped(MeasurementOut)
SymptomItem = enveloped(SymptomOut)
CheckinItem = enveloped(CheckinOut)
NoteItem = enveloped(NoteOut)
AppointmentItem = enveloped(AppointmentOut)
MemoryItem = enveloped(MemoryOut)


# --- Input schemas: unknown fields are rejected, so `422` names the field ---------------


class Strict(Schema):
    model_config = ConfigDict(extra="forbid")


class GoalIn(Strict):
    metric: str
    direction: str
    target: Decimal
    target_high: Decimal | None = None
    by: date | None = None
    review_at: date | None = None
    priority: int = 1
    status: str = "active"
    baseline_value: Decimal | None = None
    baseline_on: date | None = None
    note: str = ""


class GoalPatch(Strict):
    metric: str | None = None
    direction: str | None = None
    target: Decimal | None = None
    target_high: Decimal | None = None
    by: date | None = None
    review_at: date | None = None
    priority: int | None = None
    status: str | None = None
    baseline_value: Decimal | None = None
    baseline_on: date | None = None
    note: str | None = None


class ProgramIn(Strict):
    name: str
    phase: str = ""
    started: date | None = None
    ended: date | None = None
    sessions_per_week: int | None = None
    deload_every_weeks: int | None = None
    progression: str = ""
    workout_types: list[str] = Field(default_factory=list)


class ProgramPatch(Strict):
    name: str | None = None
    phase: str | None = None
    started: date | None = None
    ended: date | None = None
    sessions_per_week: int | None = None
    deload_every_weeks: int | None = None
    progression: str | None = None
    workout_types: list[str] | None = None


class PeriodIn(Strict):
    kind: str
    start: date
    end: date | None = None
    note: str = ""


class PeriodPatch(Strict):
    kind: str | None = None
    start: date | None = None
    end: date | None = None
    note: str | None = None


class ConditionIn(Strict):
    name: str
    code: str = ""
    since: date | None = None
    since_precision: str = "day"
    status: str = "active"
    severity: str = ""
    abatement: str = ""
    note: str = ""
    practitioner_id: int | None = None


class ConditionPatch(Strict):
    name: str | None = None
    code: str | None = None
    since: date | None = None
    since_precision: str | None = None
    status: str | None = None
    severity: str | None = None
    abatement: str | None = None
    note: str | None = None
    practitioner_id: int | None = None


class InjuryIn(Strict):
    area: str
    side: str = "na"
    since: date | None = None
    since_precision: str = "day"
    status: str = "active"
    mechanism: str = ""
    rehab: str = ""
    limits: list[LimitOut] = Field(default_factory=list)


class InjuryPatch(Strict):
    area: str | None = None
    side: str | None = None
    since: date | None = None
    since_precision: str | None = None
    status: str | None = None
    mechanism: str | None = None
    rehab: str | None = None
    limits: list[LimitOut] | None = None


class DoseEventIn(Strict):
    medication_id: int
    status: str = "taken"
    at: datetime
    scheduled_for: datetime | None = None
    dose: Decimal | None = None
    reason: str = ""


class DoseEventPatch(Strict):
    status: str | None = None
    at: datetime | None = None
    dose: Decimal | None = None
    reason: str | None = None


class ContextIn(Strict):
    posture: str | None = None
    side: str | None = None
    fasted: bool | None = None


class ComponentsIn(Strict):
    systolic: int
    diastolic: int


class MeasurementIn(Strict):
    metric: str
    value: Decimal | None = None
    unit: str | None = None
    at: datetime | None = None
    context: ContextIn = Field(default_factory=ContextIn)
    device: str = ""
    note: str = ""
    components: ComponentsIn | None = None
    pulse: int | None = None


class MeasurementPatch(Strict):
    value: Decimal | None = None
    at: datetime | None = None
    posture: str | None = None
    side: str | None = None
    fasted: bool | None = None
    device: str | None = None
    note: str | None = None


class SymptomIn(Strict):
    name: str
    severity: int | None = None
    interference: str = ""
    onset_at: datetime | None = None
    resolved_at: datetime | None = None
    body_area: str = ""
    side: str = "na"
    trigger: str = ""
    note: str = ""


class SymptomPatch(Strict):
    name: str | None = None
    severity: int | None = None
    interference: str | None = None
    onset_at: datetime | None = None
    resolved_at: datetime | None = None
    body_area: str | None = None
    side: str | None = None
    trigger: str | None = None
    note: str | None = None


class CheckinIn(Strict):
    at: datetime | None = None
    energy: int | None = None
    mood: int | None = None
    hunger: int | None = None
    soreness: int | None = None
    sleep_quality: int | None = None
    stress: int | None = None
    note: str = ""


class CheckinPatch(Strict):
    at: datetime | None = None
    energy: int | None = None
    mood: int | None = None
    hunger: int | None = None
    soreness: int | None = None
    sleep_quality: int | None = None
    stress: int | None = None
    note: str | None = None


class NoteIn(Strict):
    at: datetime | None = None
    body: str
    tags: list[str] = Field(default_factory=list)


class NotePatch(Strict):
    at: datetime | None = None
    body: str | None = None
    tags: list[str] | None = None


class AppointmentIn(Strict):
    at: datetime
    practitioner_id: int | None = None
    practitioner_name: str = ""
    specialty: str = ""
    place: str = ""
    reason: str = ""
    outcome_note: str = ""


class AppointmentPatch(Strict):
    at: datetime | None = None
    practitioner_id: int | None = None
    practitioner_name: str | None = None
    specialty: str | None = None
    place: str | None = None
    reason: str | None = None
    outcome_note: str | None = None


class MemoryIn(Strict):
    kind: str
    subject: str
    statement: str
    observed_at: datetime
    importance: int = 3
    source_ref: str = ""
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    due_at: datetime | None = None
    corrects_id: int | None = None


class MemoryPatch(Strict):
    statement: str | None = None
    subject: str | None = None
    tags: list[str] | None = None
    pinned: bool | None = None
    due_at: datetime | None = None


class EnvironmentPatch(Strict):
    country: str | None = None
    region: str | None = None
    urban_rural: str | None = None
    gym_access: str | None = None
    equipment: list[str] | None = None
    work_pattern: str | None = None
    work_load: str | None = None
    cooks_for: int | None = None
    cooking_facilities: str | None = None
    tobacco: str | None = None
    tobacco_amount: str | None = None
    alcohol: str | None = None
    alcohol_units: str | None = None
    notes: str | None = None


# --- Helpers --------------------------------------------------------------------------


def user_of(request: HttpRequest):
    return request.auth.user


def fetch(model, request, pk, *, queryset=None, path="id"):
    rows = (
        queryset if queryset is not None else model._default_manager.filter(user=user_of(request))
    )
    obj = rows.filter(pk=pk).first()
    if obj is None:
        message = "Unknown record."
        raise Problem(404, "not_found", "Not found.", [{"path": path, "message": message}])
    return obj


def reject_nulls(obj):
    for field in obj._meta.concrete_fields:
        if field.null or field.primary_key or not field.editable:
            continue
        if getattr(obj, field.attname) is None:
            raise invalid_field(field.name, "This field cannot be null.")


def validate_fields(obj):
    """Field-level checks only: a panel has no scalar metric for `clean()` to accept."""
    reject_nulls(obj)
    try:
        obj.clean_fields()
    except DjangoValidationError as exc:
        raise invalid(exc) from exc


def commit(obj):
    reject_nulls(obj)
    try:
        obj.full_clean()
        obj.save()
    except DjangoValidationError as exc:
        raise invalid(exc) from exc
    return obj


def audit(request, action, kind, object_id=""):
    M.AuditEvent._default_manager.create(
        user=user_of(request), action=action, object_kind=kind, object_id=str(object_id)
    )


def created(request, payload):
    response = api.create_response(request, payload, status=201)
    response["Location"] = f"{request.path.rstrip('/')}/{payload['id']}"
    return response


def render(schema, obj, warnings=(), with_envelope=True):
    data = schema.model_validate(obj).model_dump()
    data["warnings"] = list(warnings)
    if with_envelope:
        data.update(envelope())
    return data


def page(request, queryset, schema, limit, offset, renderer=None):
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    total = queryset.count()
    rows = list(queryset[offset : offset + limit])
    renderer = renderer or (lambda row: render(schema, row, with_envelope=False))
    return {
        **envelope(),
        "items": [renderer(row) for row in rows],
        "count": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


def window(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    end = end or today
    start = start or end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if end < start:
        raise invalid_field("end", "The end date must be on or after the start date.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise invalid_field("end", "The date range must be 1830 days or less.")
    return start, end


def filtered(request, queryset, field, start, end, kind):
    zone = S.account_timezone(user_of(request))
    if kind == "date":
        return queryset.filter(**{f"{field}__gte": start, f"{field}__lte": end})
    first = datetime.combine(start, time(0, 0), tzinfo=zone)
    after = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=zone)
    return queryset.filter(**{f"{field}__gte": first, f"{field}__lt": after})


def new_object(model, request, data, **forced):
    obj = model(
        user=user_of(request),
        source=M.Source.CLIENT,
        reported_by=request.auth.name,
        **forced,
    )
    for field, value in data.items():
        setattr(obj, field, value)
    return obj


def new_record(model, request, data, **forced):
    return commit(new_object(model, request, data, **forced))


def resolve_metric_or_422(code: str) -> R.Metric:
    try:
        return R.resolve_metric(code)
    except DjangoValidationError:
        raise invalid_field("metric", "Unknown metric.") from None


def warnings_for(metric, value, path):
    try:
        return R.check_value(metric, value)
    except DjangoValidationError:
        raise invalid_field(path, "Value is outside the accepted range.") from None


def goal_warnings(user, metric, direction, status, pk=None):
    warnings = []
    if R.METRICS[metric].authority.startswith("external:"):
        warnings.append("This metric is recorded outside healthinfo; the goal cannot track it.")
    if status == "active":
        others = M.Goal._default_manager.filter(user=user, metric=metric, status="active").exclude(
            pk=pk
        )
        if any(other.direction != direction for other in others):
            warnings.append("Another active goal on this metric has the opposite direction.")
    return warnings


def practitioner_for(request, practitioner_id):
    if practitioner_id is None:
        return None
    return fetch(M.Practitioner, request, practitioner_id, path="practitioner_id")


def apply_practitioner(request, obj, data):
    if "practitioner_id" in data:
        obj.practitioner = practitioner_for(request, data.pop("practitioner_id"))


# --- Collections ----------------------------------------------------------------------


def handle_list(request, model, schema, limit, offset, date_field=None, date_kind="datetime"):
    queryset = model._default_manager.filter(user=user_of(request))
    if date_field is None:
        return page(request, queryset, schema, limit, offset)
    start, end = parse_window(request)
    queryset = filtered(request, queryset, date_field, start, end, date_kind)
    return page(request, queryset, schema, limit, offset)


def parse_window(request):
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET["start"]) if request.GET.get("start") else None
        end = date.fromisoformat(request.GET["end"]) if request.GET.get("end") else None
    except ValueError:
        raise invalid_field("start", "Dates are YYYY-MM-DD.") from None
    return window(start, end, today)


# --- Person and environment -----------------------------------------------------------


@api.get("/person", response=PersonEnvelope, tags=["context"])
def get_person(request):
    person = M.Person._default_manager.filter(user=user_of(request)).first() or M.Person()
    payload = PersonOut.model_validate(person).model_dump()
    return {**envelope(), "person": payload}


@api.get("/environment", response=EnvironmentEnvelope, tags=["context"])
def get_environment(request):
    return _environment(request)


@api.patch("/environment", response=EnvironmentEnvelope, tags=["context"])
def patch_environment(request, payload: EnvironmentPatch):
    data = payload.model_dump(exclude_unset=True)
    if data.get("equipment") is not None:
        for item in data["equipment"]:
            if item not in R.EQUIPMENT:
                raise invalid_field("equipment", "Unknown equipment.")
    environment = M.Environment._default_manager.filter(user=user_of(request)).first()
    if environment is None:
        environment = M.Environment(user=user_of(request))
    for field, value in data.items():
        setattr(environment, field, value)
    if environment.equipment is None:
        environment.equipment = []
    commit(environment)
    audit(request, "update", "records.Environment", environment.pk)
    return _environment(request)


def _environment(request):
    environment = M.Environment._default_manager.filter(user=user_of(request)).first() or (
        M.Environment(user=user_of(request))
    )
    payload = EnvironmentOut.model_validate(environment).model_dump()
    payload["equipment"] = list(environment.equipment or [])
    payload["state"] = M.domain_state(user_of(request), "environment")
    return {**envelope(), "environment": payload}


# --- Writable collections -------------------------------------------------------------


@api.get("/goals", response=Page[GoalOut], tags=["goals"])
def list_goals(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Goal, GoalOut, limit, offset)


@api.post("/goals", response={201: GoalItem}, tags=["goals"])
def create_goal(request, payload: GoalIn):
    data = payload.model_dump()
    metric = resolve_metric_or_422(data.pop("metric"))
    data["metric"] = metric.code
    obj = new_record(M.Goal, request, data)
    audit(request, "create", "records.Goal", obj.pk)
    return created(
        request,
        render(
            GoalOut,
            obj,
            goal_warnings(user_of(request), obj.metric, obj.direction, obj.status, obj.pk),
        ),
    )


@api.get("/goals/{id}", response=GoalItem, tags=["goals"])
def get_goal(request, id: int):
    return render(GoalOut, fetch(M.Goal, request, id))


@api.patch("/goals/{id}", response=GoalItem, tags=["goals"])
def update_goal(request, id: int, payload: GoalPatch):
    obj = fetch(M.Goal, request, id)
    data = payload.model_dump(exclude_unset=True)
    if "metric" in data and data["metric"] is not None:
        data["metric"] = resolve_metric_or_422(data["metric"]).code
    for field, value in data.items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Goal", obj.pk)
    return render(
        GoalOut, obj, goal_warnings(user_of(request), obj.metric, obj.direction, obj.status, obj.pk)
    )


@api.delete("/goals/{id}", response={204: None}, tags=["goals"])
def delete_goal(request, id: int):
    fetch(M.Goal, request, id).delete()
    audit(request, "delete", "records.Goal", id)
    return HttpResponse(status=204)


@api.get("/programs", response=Page[ProgramOut], tags=["program"])
def list_programs(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Program, ProgramOut, limit, offset)


@api.post("/programs", response={201: ProgramItem}, tags=["program"])
def create_program(request, payload: ProgramIn):
    data = payload.model_dump()
    data["workout_types"] = [
        R.normalise_workout_type(item) for item in data["workout_types"] if item.strip()
    ]
    obj = new_record(M.Program, request, data)
    audit(request, "create", "records.Program", obj.pk)
    return created(request, render(ProgramOut, obj))


@api.get("/programs/{id}", response=ProgramItem, tags=["program"])
def get_program(request, id: int):
    return render(ProgramOut, fetch(M.Program, request, id))


@api.patch("/programs/{id}", response=ProgramItem, tags=["program"])
def update_program(request, id: int, payload: ProgramPatch):
    obj = fetch(M.Program, request, id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("workout_types") is not None:
        data["workout_types"] = [
            R.normalise_workout_type(item) for item in data["workout_types"] if item.strip()
        ]
    for field, value in data.items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Program", obj.pk)
    return render(ProgramOut, obj)


@api.delete("/programs/{id}", response={204: None}, tags=["program"])
def delete_program(request, id: int):
    fetch(M.Program, request, id).delete()
    audit(request, "delete", "records.Program", id)
    return HttpResponse(status=204)


@api.get("/periods", response=Page[PeriodOut], tags=["periods"])
def list_periods(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Period, PeriodOut, limit, offset, "start", "date")


@api.post("/periods", response={201: PeriodItem}, tags=["periods"])
def create_period(request, payload: PeriodIn):
    obj = new_record(M.Period, request, payload.model_dump())
    audit(request, "create", "records.Period", obj.pk)
    return created(request, render(PeriodOut, obj))


@api.get("/periods/{id}", response=PeriodItem, tags=["periods"])
def get_period(request, id: int):
    return render(PeriodOut, fetch(M.Period, request, id))


@api.patch("/periods/{id}", response=PeriodItem, tags=["periods"])
def update_period(request, id: int, payload: PeriodPatch):
    obj = fetch(M.Period, request, id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Period", obj.pk)
    return render(PeriodOut, obj)


@api.delete("/periods/{id}", response={204: None}, tags=["periods"])
def delete_period(request, id: int):
    fetch(M.Period, request, id).delete()
    audit(request, "delete", "records.Period", id)
    return HttpResponse(status=204)


@api.get("/conditions", response=Page[ConditionOut], tags=["conditions"])
def list_conditions(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Condition, ConditionOut, limit, offset)


@api.post("/conditions", response={201: ConditionItem}, tags=["conditions"])
def create_condition(request, payload: ConditionIn):
    data = payload.model_dump()
    practitioner = practitioner_for(request, data.pop("practitioner_id"))
    obj = new_object(M.Condition, request, data, verification_status="unconfirmed")
    obj.practitioner = practitioner
    commit(obj)
    audit(request, "create", "records.Condition", obj.pk)
    return created(request, render(ConditionOut, obj))


@api.get("/conditions/{id}", response=ConditionItem, tags=["conditions"])
def get_condition(request, id: int):
    return render(ConditionOut, fetch(M.Condition, request, id))


@api.patch("/conditions/{id}", response=ConditionItem, tags=["conditions"])
def update_condition(request, id: int, payload: ConditionPatch):
    obj = fetch(M.Condition, request, id)
    data = payload.model_dump(exclude_unset=True)
    apply_practitioner(request, obj, data)
    for field, value in data.items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Condition", obj.pk)
    return render(ConditionOut, obj)


@api.delete("/conditions/{id}", response={204: None}, tags=["conditions"])
def delete_condition(request, id: int):
    fetch(M.Condition, request, id).delete()
    audit(request, "delete", "records.Condition", id)
    return HttpResponse(status=204)


@api.get("/injuries", response=Page[InjuryOut], tags=["injuries"])
def list_injuries(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Injury, InjuryOut, limit, offset)


@api.post("/injuries", response={201: InjuryItem}, tags=["injuries"])
def create_injury(request, payload: InjuryIn):
    data = payload.model_dump()
    data["limits"] = _limits(payload.limits)
    _check_limits(data["limits"])
    obj = new_object(M.Injury, request, data, verification_status="unconfirmed")
    commit(obj)
    audit(request, "create", "records.Injury", obj.pk)
    return created(request, render(InjuryOut, obj))


@api.get("/injuries/{id}", response=InjuryItem, tags=["injuries"])
def get_injury(request, id: int):
    return render(InjuryOut, fetch(M.Injury, request, id))


@api.patch("/injuries/{id}", response=InjuryItem, tags=["injuries"])
def update_injury(request, id: int, payload: InjuryPatch):
    obj = fetch(M.Injury, request, id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("limits") is not None:
        data["limits"] = _limits(payload.limits)
        _check_limits(data["limits"])
    for field, value in data.items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Injury", obj.pk)
    return render(InjuryOut, obj)


@api.delete("/injuries/{id}", response={204: None}, tags=["injuries"])
def delete_injury(request, id: int):
    fetch(M.Injury, request, id).delete()
    audit(request, "delete", "records.Injury", id)
    return HttpResponse(status=204)


def _limits(limits):
    return [limit.model_dump(exclude_none=True) for limit in limits]


def _check_limits(limits):
    for limit in limits:
        if limit.get("pattern") not in R.MOVEMENT_PATTERNS:
            raise invalid_field("limits", "Unknown movement pattern.")
        if limit.get("kind") not in R.LIMIT_KIND:
            raise invalid_field("limits", "A limit is avoid or caution.")


@api.get("/dose-events", response=Page[DoseEventOut], tags=["doses"])
def list_dose_events(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.DoseEvent, DoseEventOut, limit, offset, "at")


@api.post("/dose-events", response={200: DoseEventItem, 201: DoseEventItem}, tags=["doses"])
def create_dose_event(request, payload: DoseEventIn):
    data = payload.model_dump()
    medication = fetch(M.Medication, request, data.pop("medication_id"), path="medication_id")
    scheduled_for = data.get("scheduled_for")
    if scheduled_for is not None:
        existing = M.DoseEvent._default_manager.filter(
            user=user_of(request), medication=medication, scheduled_for=scheduled_for
        ).first()
        if existing is not None:
            # A retried tap is a no-op, not a second dose.
            return render(DoseEventOut, existing)
    obj = new_record(M.DoseEvent, request, data, medication=medication)
    audit(request, "create", "records.DoseEvent", obj.pk)
    return created(request, render(DoseEventOut, obj))


@api.get("/dose-events/{id}", response=DoseEventItem, tags=["doses"])
def get_dose_event(request, id: int):
    return render(DoseEventOut, fetch(M.DoseEvent, request, id))


@api.patch("/dose-events/{id}", response=DoseEventItem, tags=["doses"])
def update_dose_event(request, id: int, payload: DoseEventPatch):
    obj = fetch(M.DoseEvent, request, id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.DoseEvent", obj.pk)
    return render(DoseEventOut, obj)


@api.delete("/dose-events/{id}", response={204: None}, tags=["doses"])
def delete_dose_event(request, id: int):
    fetch(M.DoseEvent, request, id).delete()
    audit(request, "delete", "records.DoseEvent", id)
    return HttpResponse(status=204)


@api.get("/measurements", response=Page[MeasurementOut], tags=["measurements"])
def list_measurements(request, limit: int = 50, offset: int = 0):
    start, end = parse_window(request)
    queryset = filtered(
        request,
        M.Measurement._default_manager.filter(user=user_of(request), parent__isnull=True),
        "at",
        start,
        end,
        "datetime",
    ).prefetch_related("components")
    return page(request, queryset, MeasurementOut, limit, offset)


@api.post("/measurements", response={201: MeasurementItem}, tags=["measurements"])
def create_measurement(request, payload: MeasurementIn):
    metric = resolve_metric_or_422(payload.metric)
    at = payload.at or timezone.now()
    if metric.code == "blood_pressure":
        return _create_blood_pressure(request, payload, at)
    if payload.components is not None:
        raise invalid_field("components", "Only a blood pressure reading has components.")
    if payload.pulse is not None:
        raise invalid_field("pulse", "Pulse is logged as its own measurement.")
    if "log" not in metric.use:
        raise invalid_field("metric", "This metric cannot be measured.")
    if payload.value is None:
        raise invalid_field("value", "A measurement needs a value.")
    if payload.unit and payload.unit != metric.unit:
        raise Problem(
            422,
            "unit_mismatch",
            "Unit does not match the metric.",
            [{"path": "unit", "message": "Unit does not match the metric."}],
        )
    warnings = warnings_for(metric, payload.value, "value")
    context = payload.context.model_dump()
    _check_context(metric, context)
    obj = M.Measurement(
        user=user_of(request),
        source=M.Source.CLIENT,
        reported_by=request.auth.name,
        metric=metric.code,
        value=payload.value,
        unit=metric.unit,
        at=at,
        device=payload.device,
        note=payload.note,
        **{key: value for key, value in context.items() if value is not None},
    )
    commit(obj)
    audit(request, "create", "records.Measurement", obj.pk)
    return created(request, render(MeasurementOut, obj, warnings))


def _check_context(metric, context):
    for name in ("posture", "side", "fasted"):
        value = context.get(name)
        if value is not None and name not in metric.contexts:
            raise invalid_field(f"context.{name}", "This metric does not take that context.")


@transaction.atomic
def _create_blood_pressure(request, payload: MeasurementIn, at):
    if payload.value is not None:
        raise invalid_field("value", "A blood pressure reading carries components, not a value.")
    if payload.components is None:
        raise invalid_field("components", "A blood pressure reading needs systolic and diastolic.")
    context = payload.context.model_dump()
    for name, allowed in (("posture", R.POSTURE), ("side", R.SIDE)):
        value = context.get(name)
        if value is not None and value not in allowed:
            raise invalid_field(f"context.{name}", "Unknown value.")
    if context.get("fasted") is not None:
        raise invalid_field("context.fasted", "Blood pressure does not take the fasted context.")
    readings = {
        "systolic_bp": payload.components.systolic,
        "diastolic_bp": payload.components.diastolic,
    }
    warnings = []
    for code, value in readings.items():
        path = "components." + code.split("_")[0]
        warnings += warnings_for(R.METRICS[code], value, path)
    if payload.pulse is not None:
        warnings += warnings_for(R.METRICS["pulse"], payload.pulse, "pulse")

    user, name = user_of(request), request.auth.name
    panel = M.Measurement._default_manager.create(
        user=user,
        source=M.Source.CLIENT,
        reported_by=name,
        metric="blood_pressure",
        at=at,
        note=payload.note,
        device=payload.device,
        posture=context.get("posture") or "",
        side=context.get("side") or "",
    )
    for code, value in readings.items():
        metric = R.METRICS[code]
        M.Measurement._default_manager.create(
            user=user,
            source=M.Source.CLIENT,
            reported_by=name,
            metric=code,
            value=value,
            unit=metric.unit,
            at=at,
            parent=panel,
            posture=context.get("posture") or "",
            side=context.get("side") or "",
        )
    if payload.pulse is not None:
        M.Measurement._default_manager.create(
            user=user,
            source=M.Source.CLIENT,
            reported_by=name,
            metric="pulse",
            value=payload.pulse,
            unit=R.METRICS["pulse"].unit,
            at=at,
        )
    audit(request, "create", "records.Measurement", panel.pk)
    return created(request, render(MeasurementOut, panel, warnings))


@api.get("/measurements/{id}", response=MeasurementItem, tags=["measurements"])
def get_measurement(request, id: int):
    obj = fetch(
        M.Measurement,
        request,
        id,
        queryset=M.Measurement._default_manager.filter(user=user_of(request), parent__isnull=True),
    )
    return render(MeasurementOut, obj)


@api.patch("/measurements/{id}", response=MeasurementItem, tags=["measurements"])
def update_measurement(request, id: int, payload: MeasurementPatch):
    obj = fetch(
        M.Measurement,
        request,
        id,
        queryset=M.Measurement._default_manager.filter(user=user_of(request), parent__isnull=True),
    )
    data = payload.model_dump(exclude_unset=True)
    if obj.metric == "blood_pressure":
        for field in data:
            if field not in ("at", "note"):
                raise invalid_field(field, "A blood pressure panel carries only its components.")
        for field, value in data.items():
            setattr(obj, field, value)
        validate_fields(obj)
        with transaction.atomic():
            obj.save()
            if "at" in data:
                obj.components.update(at=obj.at)
        audit(request, "update", "records.Measurement", obj.pk)
        return render(MeasurementOut, obj)
    warnings = ()
    if data.get("value") is not None:
        warnings = warnings_for(R.resolve_metric(obj.metric), data["value"], "value")
    for field, value in data.items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Measurement", obj.pk)
    return render(MeasurementOut, obj, warnings)


@api.delete("/measurements/{id}", response={204: None}, tags=["measurements"])
def delete_measurement(request, id: int):
    obj = fetch(M.Measurement, request, id)
    if obj.parent_id:
        raise Problem(
            404, "not_found", "Not found.", [{"path": "id", "message": "Unknown record."}]
        )
    obj.delete()
    audit(request, "delete", "records.Measurement", id)
    return HttpResponse(status=204)


@api.get("/symptoms", response=Page[SymptomOut], tags=["symptoms"])
def list_symptoms(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Symptom, SymptomOut, limit, offset, "onset_at")


@api.post("/symptoms", response={201: SymptomItem}, tags=["symptoms"])
def create_symptom(request, payload: SymptomIn):
    data = payload.model_dump()
    data["onset_at"] = data["onset_at"] or timezone.now()
    obj = new_record(M.Symptom, request, data)
    audit(request, "create", "records.Symptom", obj.pk)
    return created(request, render(SymptomOut, obj))


@api.get("/symptoms/{id}", response=SymptomItem, tags=["symptoms"])
def get_symptom(request, id: int):
    return render(SymptomOut, fetch(M.Symptom, request, id))


@api.patch("/symptoms/{id}", response=SymptomItem, tags=["symptoms"])
def update_symptom(request, id: int, payload: SymptomPatch):
    obj = fetch(M.Symptom, request, id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Symptom", obj.pk)
    return render(SymptomOut, obj)


@api.delete("/symptoms/{id}", response={204: None}, tags=["symptoms"])
def delete_symptom(request, id: int):
    fetch(M.Symptom, request, id).delete()
    audit(request, "delete", "records.Symptom", id)
    return HttpResponse(status=204)


@api.get("/checkins", response=Page[CheckinOut], tags=["checkins"])
def list_checkins(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Checkin, CheckinOut, limit, offset, "at")


@api.post("/checkins", response={201: CheckinItem}, tags=["checkins"])
def create_checkin(request, payload: CheckinIn):
    data = payload.model_dump()
    data["at"] = data["at"] or timezone.now()
    obj = new_record(M.Checkin, request, data)
    audit(request, "create", "records.Checkin", obj.pk)
    return created(request, render(CheckinOut, obj))


@api.get("/checkins/{id}", response=CheckinItem, tags=["checkins"])
def get_checkin(request, id: int):
    return render(CheckinOut, fetch(M.Checkin, request, id))


@api.patch("/checkins/{id}", response=CheckinItem, tags=["checkins"])
def update_checkin(request, id: int, payload: CheckinPatch):
    obj = fetch(M.Checkin, request, id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Checkin", obj.pk)
    return render(CheckinOut, obj)


@api.delete("/checkins/{id}", response={204: None}, tags=["checkins"])
def delete_checkin(request, id: int):
    fetch(M.Checkin, request, id).delete()
    audit(request, "delete", "records.Checkin", id)
    return HttpResponse(status=204)


@api.get("/notes", response=Page[NoteOut], tags=["notes"])
def list_notes(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Note, NoteOut, limit, offset, "at")


@api.post("/notes", response={201: NoteItem}, tags=["notes"])
def create_note(request, payload: NoteIn):
    data = payload.model_dump()
    data["at"] = data["at"] or timezone.now()
    obj = new_record(M.Note, request, data)
    audit(request, "create", "records.Note", obj.pk)
    return created(request, render(NoteOut, obj))


@api.get("/notes/{id}", response=NoteItem, tags=["notes"])
def get_note(request, id: int):
    return render(NoteOut, fetch(M.Note, request, id))


@api.patch("/notes/{id}", response=NoteItem, tags=["notes"])
def update_note(request, id: int, payload: NotePatch):
    obj = fetch(M.Note, request, id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Note", obj.pk)
    return render(NoteOut, obj)


@api.delete("/notes/{id}", response={204: None}, tags=["notes"])
def delete_note(request, id: int):
    fetch(M.Note, request, id).delete()
    audit(request, "delete", "records.Note", id)
    return HttpResponse(status=204)


@api.get("/appointments", response=Page[AppointmentOut], tags=["appointments"])
def list_appointments(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Appointment, AppointmentOut, limit, offset, "at")


@api.post("/appointments", response={201: AppointmentItem}, tags=["appointments"])
def create_appointment(request, payload: AppointmentIn):
    data = payload.model_dump()
    practitioner = practitioner_for(request, data.pop("practitioner_id"))
    obj = new_object(M.Appointment, request, data, status="planned")
    obj.practitioner = practitioner
    commit(obj)
    audit(request, "create", "records.Appointment", obj.pk)
    return created(request, render(AppointmentOut, obj))


@api.get("/appointments/{id}", response=AppointmentItem, tags=["appointments"])
def get_appointment(request, id: int):
    return render(AppointmentOut, fetch(M.Appointment, request, id))


@api.patch("/appointments/{id}", response=AppointmentItem, tags=["appointments"])
def update_appointment(request, id: int, payload: AppointmentPatch):
    obj = fetch(M.Appointment, request, id)
    data = payload.model_dump(exclude_unset=True)
    apply_practitioner(request, obj, data)
    for field, value in data.items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.Appointment", obj.pk)
    return render(AppointmentOut, obj)


@api.delete("/appointments/{id}", response={204: None}, tags=["appointments"])
def delete_appointment(request, id: int):
    fetch(M.Appointment, request, id).delete()
    audit(request, "delete", "records.Appointment", id)
    return HttpResponse(status=204)


@api.get("/memory", response=Page[MemoryOut], tags=["memory"])
def list_memory(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.MemoryEntry, MemoryOut, limit, offset, "observed_at")


@api.post("/memory", response={201: MemoryItem}, tags=["memory"])
def create_memory(request, payload: MemoryIn):
    data = payload.model_dump()
    corrects_id = data.pop("corrects_id")
    if corrects_id is None:
        obj = new_record(M.MemoryEntry, request, data)
    else:
        old = fetch(M.MemoryEntry, request, corrects_id, path="corrects_id")
        if old.locked:
            raise Problem(
                422,
                "locked",
                "This entry is locked.",
                [{"path": "corrects_id", "message": "Locked."}],
            )
        entry = M.MemoryEntry(
            user=user_of(request),
            source=M.Source.CLIENT,
            reported_by=request.auth.name,
            corrects=old,
            kind="correction",
        )
        for field, value in data.items():
            setattr(entry, field, value)
        try:
            entry.full_clean()
        except DjangoValidationError as exc:
            raise invalid(exc) from exc
        try:
            obj = M.supersede(old, entry)
        except DjangoValidationError as exc:
            raise invalid(exc) from exc
    audit(request, "create", "records.MemoryEntry", obj.pk)
    return created(request, render(MemoryOut, obj))


@api.get("/memory/{id}", response=MemoryItem, tags=["memory"])
def get_memory(request, id: int):
    return render(MemoryOut, fetch(M.MemoryEntry, request, id))


@api.patch("/memory/{id}", response=MemoryItem, tags=["memory"])
def update_memory(request, id: int, payload: MemoryPatch):
    obj = fetch(M.MemoryEntry, request, id)
    if obj.locked:
        raise Problem(
            422, "locked", "This entry is locked.", [{"path": "id", "message": "Locked."}]
        )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    commit(obj)
    audit(request, "update", "records.MemoryEntry", obj.pk)
    return render(MemoryOut, obj)


@api.delete("/memory/{id}", response={204: None}, tags=["memory"])
def delete_memory(request, id: int):
    obj = fetch(M.MemoryEntry, request, id)
    if obj.locked:
        raise Problem(
            422, "locked", "This entry is locked.", [{"path": "id", "message": "Locked."}]
        )
    try:
        obj.delete()
    except ProtectedError:
        raise Problem(
            409,
            "has_corrections",
            "A newer entry corrects this one.",
            [{"path": "id", "message": "Delete the correction first."}],
        ) from None
    audit(request, "delete", "records.MemoryEntry", id)
    return HttpResponse(status=204)


# --- Read-only collections ------------------------------------------------------------


@api.get("/allergies", response=Page[AllergyOut], tags=["allergies"])
def list_allergies(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Allergy, AllergyOut, limit, offset)


@api.get("/medications", response=Page[MedicationOut], tags=["medications"])
def list_medications(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Medication, MedicationOut, limit, offset)


@api.get("/labs", response=Page[LabOut], tags=["labs"])
def list_labs(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Lab, LabOut, limit, offset, "collected_at")


@api.get("/vaccinations", response=Page[VaccinationOut], tags=["vaccinations"])
def list_vaccinations(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Vaccination, VaccinationOut, limit, offset)


@api.get("/practitioners", response=Page[PractitionerOut], tags=["practitioners"])
def list_practitioners(request, limit: int = 50, offset: int = 0):
    return handle_list(request, M.Practitioner, PractitionerOut, limit, offset)


# --- Derived reads --------------------------------------------------------------------


@api.get("/expected-doses", response=Page[ExpectedDoseOut], tags=["doses"])
def expected_doses(request, limit: int = 50, offset: int = 0):
    start, end = parse_window(request)
    slots = S.expected_doses_for_user(user_of(request), start, end)
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    items = [
        ExpectedDoseOut.model_validate(slot).model_dump() for slot in slots[offset : offset + limit]
    ]
    return {
        **envelope(),
        "items": items,
        "count": len(slots),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < len(slots),
    }


@api.get("/adherence", response=AdherenceOut, tags=["doses"])
def adherence(request, medication_id: int | None = None):
    start, end = parse_window(request)
    medication = None
    if medication_id is not None:
        medication = fetch(M.Medication, request, medication_id, path="medication_id")
    return {**envelope(), **S.adherence(user_of(request), start, end, medication)}


@api.get("/summary", response=SummaryEnvelope, tags=["summary"])
def summary(request):
    return {**envelope(), "summary": build_summary(request)}


def build_summary(request):
    user = user_of(request)
    person = M.Person._default_manager.filter(user=user).first()
    environment = M.Environment._default_manager.filter(user=user).first()
    program = (
        M.Program._default_manager.filter(user=user).order_by("-started", "-created_at").first()
    )
    goals = M.Goal._default_manager.filter(user=user, status="active").order_by("priority", "by")
    active_courses = list(M.Medication._default_manager.filter(user=user, stopped__isnull=True))
    last_measurement = (
        M.Measurement._default_manager.filter(user=user, parent__isnull=True)
        .order_by("-at")
        .first()
    )
    last_lab = M.Lab._default_manager.filter(user=user).order_by("-collected_at").first()
    zone = S.account_timezone(user)

    def local_date(value):
        return timezone.localtime(value, zone).date() if value else None

    def block(domain, model, items, schema):
        return {
            "state": M.domain_state(user, domain),
            "items": [schema.model_validate(item).model_dump() for item in items],
        }

    return {
        "person": {
            "age_years": person.age_years if person else None,
            "sex_at_birth": person.sex_at_birth if person else "unknown",
            "height_cm": person.height_cm if person else None,
            "timezone": person.timezone if person else settings.TIME_ZONE,
        },
        "goals": [SummaryGoal.model_validate(goal).model_dump() for goal in goals],
        "program": SummaryProgram.model_validate(program).model_dump() if program else None,
        "environment": {
            "gym_access": environment.gym_access if environment else "",
            "work_pattern": environment.work_pattern if environment else "unknown",
        },
        "constraints": {
            "allergies": block(
                "allergies", M.Allergy, M.Allergy._default_manager.filter(user=user), SummaryAllergy
            ),
            "injuries": block(
                "injuries", M.Injury, M.Injury._default_manager.filter(user=user), SummaryInjury
            ),
            "conditions": block(
                "conditions",
                M.Condition,
                M.Condition._default_manager.filter(user=user),
                SummaryCondition,
            ),
        },
        "regimen": {
            "active_medications": len(active_courses),
            "indication_recorded": bool(active_courses)
            and all(course.indication.strip() for course in active_courses),
        },
        "availability": {
            "labs": {
                "state": M.domain_state(user, "labs"),
                "count": M.Lab._default_manager.filter(user=user).count(),
                "last_at": local_date(last_lab.collected_at) if last_lab else None,
            },
            "measurements": {
                "state": M.domain_state(user, "measurements"),
                "count": M.Measurement._default_manager.filter(
                    user=user, parent__isnull=True
                ).count(),
                "last_at": local_date(last_measurement.at) if last_measurement else None,
            },
        },
        "missing": [domain for domain in R.DOMAINS if M.domain_state(user, domain) == "unknown"],
        "memory": {
            "pinned": M.MemoryEntry._default_manager.filter(user=user, pinned=True).count(),
            "open_follow_ups": M.MemoryEntry._default_manager.filter(
                user=user, kind="follow_up", status="active"
            ).count(),
        },
    }


@api.get("/health", response=HealthOut, tags=["health"])
def health(request):
    user = user_of(request)
    counts = {
        domain: model._default_manager.filter(user=user).count()
        for domain, model in M.DOMAIN_MODELS.items()
    }
    counts["dose_events"] = M.DoseEvent._default_manager.filter(user=user).count()
    return {
        **envelope(),
        "status": "ok",
        "version": settings.APP_VERSION,
        "today": timezone.localdate(),
        "counts": counts,
    }
