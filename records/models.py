"""The data model: owner-scoped records, current-state rows, no revisions or tombstones."""

import hashlib
import secrets
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from records import registries as R


def _plain(value):
    """A Decimal as plain digits: no exponent, no trailing zeros."""
    return format(value.normalize(), "f")


class Source:
    OWNER, CLIENT, IMPORT, DEVICE = R.SOURCE


class OwnedRecord(models.Model):
    """Every health or memory row: whose it is, who reported it, when it changed."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, editable=False, related_name="+"
    )
    source = models.CharField(max_length=16, choices=R.choices(R.SOURCE), default=Source.OWNER)
    reported_by = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]

    @property
    def is_client_written(self):
        return self.source == Source.CLIENT


class Person(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="person"
    )
    name = models.CharField(max_length=200, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    birth_date_precision = models.CharField(
        max_length=6, choices=R.choices(R.DATE_PRECISION), default="day"
    )
    sex_at_birth = models.CharField(
        max_length=8, choices=R.choices(R.SEX_AT_BIRTH), default="unknown"
    )
    gender = models.CharField(max_length=100, blank=True)
    height_cm = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/Stockholm")
    unit_system = models.CharField(max_length=8, choices=R.choices(R.UNIT_SYSTEM), default="metric")
    locale = models.CharField(max_length=16, blank=True, default="en")

    class Meta:
        verbose_name_plural = "person"
        constraints = [
            models.CheckConstraint(
                condition=Q(height_cm__gt=0) | Q(height_cm__isnull=True),
                name="person_height_positive",
            )
        ]

    def __str__(self):
        return self.name or self.user.get_username()

    @property
    def age_years(self):
        if not self.birth_date:
            return None
        from datetime import date

        today = date.today()
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years


class DomainState(models.Model):
    """Explicit 'not asked yet' or 'none'; 'present' is always derived from rows."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    domain = models.CharField(max_length=16, choices=R.choices(R.DOMAINS))
    state = models.CharField(max_length=8, choices=R.choices(R.DOMAIN_STATE), default="unknown")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain"]
        constraints = [
            models.UniqueConstraint(fields=["user", "domain"], name="uniq_domain_state"),
            models.CheckConstraint(
                condition=Q(state__in=["unknown", "none"]), name="domain_state_explicit"
            ),
        ]

    def __str__(self):
        return f"{self.domain}: {self.state}"


class ApiToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=16, unique=True)
    key_hash = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_active_token_name",
            )
        ]

    def __str__(self):
        return self.name

    @classmethod
    def issue(cls, user, name, expires_at=None):
        """Return the row and the one-time secret; only the digest is stored."""
        raw = "hi_" + secrets.token_urlsafe(32)
        token = cls._default_manager.create(
            user=user,
            name=name,
            prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=expires_at,
        )
        return token, raw

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])


class AuditEntry(models.Model):
    """Counts and file names only; never a value, an attempted name or a token prefix."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=64)
    object_kind = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.action} {self.object_kind}"


class Environment(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    country = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    urban_rural = models.CharField(
        max_length=8, choices=R.choices(R.URBAN_RURAL), default="unknown"
    )
    gym_access = models.CharField(max_length=8, choices=R.choices(R.GYM_ACCESS), blank=True)
    equipment = models.JSONField(default=list, blank=True)
    work_pattern = models.CharField(
        max_length=8, choices=R.choices(R.WORK_PATTERN), default="unknown"
    )
    work_load = models.CharField(max_length=8, choices=R.choices(R.WORK_LOAD), default="unknown")
    cooks_for = models.PositiveSmallIntegerField(null=True, blank=True)
    cooking_facilities = models.CharField(
        max_length=8, choices=R.choices(R.COOKING_FACILITIES), blank=True
    )
    tobacco = models.CharField(max_length=8, choices=R.choices(R.TOBACCO), blank=True)
    tobacco_amount = models.CharField(max_length=100, blank=True)
    alcohol = models.CharField(max_length=12, choices=R.choices(R.ALCOHOL), blank=True)
    alcohol_units = models.CharField(max_length=100, blank=True)
    notes = models.TextField(max_length=2000, blank=True)

    def __str__(self):
        return "Environment"


class Goal(OwnedRecord):
    metric = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=R.choices(R.GOAL_DIRECTION))
    target = models.DecimalField(max_digits=9, decimal_places=3)
    target_high = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)
    by = models.DateField(null=True, blank=True)
    review_at = models.DateField(null=True, blank=True)
    priority = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=R.choices(R.GOAL_STATUS), default="active")
    baseline_value = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)
    baseline_on = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta(OwnedRecord.Meta):
        constraints = [
            models.CheckConstraint(condition=Q(priority__gte=1), name="goal_priority_min"),
        ]

    def clean(self):
        metric = R.resolve_metric(self.metric)
        if "goal" not in metric.use:
            raise ValidationError({"metric": "This metric cannot be a goal."})
        self.metric = metric.code

    def __str__(self):
        return f"{self.metric} {self.direction} {self.target}"


class Program(OwnedRecord):
    name = models.CharField(max_length=200)
    phase = models.CharField(max_length=100, blank=True)
    started = models.DateField(null=True, blank=True)
    ended = models.DateField(null=True, blank=True)
    sessions_per_week = models.PositiveSmallIntegerField(null=True, blank=True)
    deload_every_weeks = models.PositiveSmallIntegerField(null=True, blank=True)
    progression = models.TextField(blank=True)
    workout_types = models.JSONField(default=list, blank=True)

    class Meta(OwnedRecord.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(ended__gte=F("started"))
                | Q(ended__isnull=True)
                | Q(started__isnull=True),
                name="program_ended_after_started",
            )
        ]

    def __str__(self):
        return self.name


class Event(OwnedRecord):
    kind = models.CharField(max_length=12, choices=R.choices(R.EVENT_KIND))
    start = models.DateField()
    end = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta(OwnedRecord.Meta):
        indexes = [models.Index(fields=["user", "start"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(end__gte=F("start")) | Q(end__isnull=True),
                name="event_end_after_start",
            )
        ]

    def __str__(self):
        return f"{self.kind} {self.start}"


class Practitioner(OwnedRecord):
    name = models.CharField(max_length=200)
    specialty = models.CharField(max_length=100, blank=True)
    organisation = models.CharField(max_length=200, blank=True)
    contact = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return self.name


class Condition(OwnedRecord):
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=32, blank=True)
    since = models.DateField(null=True, blank=True)
    since_precision = models.CharField(
        max_length=6, choices=R.choices(R.DATE_PRECISION), default="day"
    )
    status = models.CharField(
        max_length=14, choices=R.choices(R.CONDITION_STATUS), default="active"
    )
    verification_status = models.CharField(
        max_length=12, choices=R.choices(R.VERIFICATION_STATUS), default="confirmed"
    )
    severity = models.CharField(max_length=8, choices=R.choices(R.SEVERITY), blank=True)
    abatement = models.TextField(blank=True)
    note = models.TextField(blank=True)
    practitioner = models.ForeignKey(
        Practitioner, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OwnedRecord.Meta):
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return self.name


class Injury(OwnedRecord):
    area = models.CharField(max_length=16, choices=R.choices(R.BODY_AREAS))
    side = models.CharField(max_length=6, choices=R.choices(R.BODY_SIDE), default="na")
    since = models.DateField(null=True, blank=True)
    since_precision = models.CharField(
        max_length=6, choices=R.choices(R.DATE_PRECISION), default="day"
    )
    status = models.CharField(max_length=10, choices=R.choices(R.INJURY_STATUS), default="active")
    verification_status = models.CharField(
        max_length=12, choices=R.choices(R.VERIFICATION_STATUS), default="confirmed"
    )
    mechanism = models.TextField(blank=True)
    rehab = models.TextField(blank=True)
    limits = models.JSONField(default=list, blank=True)

    class Meta(OwnedRecord.Meta):
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return R.area_label(self.area, self.side)


class Allergy(OwnedRecord):
    substance = models.CharField(max_length=200)
    type = models.CharField(max_length=12, choices=R.choices(R.ALLERGY_TYPE), default="allergy")
    reaction = models.TextField(blank=True)
    severity = models.CharField(max_length=12, choices=R.choices(R.ALLERGY_SEVERITY), blank=True)
    criticality = models.CharField(
        max_length=18, choices=R.choices(R.ALLERGY_CRITICALITY), blank=True
    )
    note = models.TextField(blank=True)

    def __str__(self):
        return self.substance


class Medication(OwnedRecord):
    kind = models.CharField(
        max_length=10, choices=R.choices(R.MEDICATION_KIND), default="medication"
    )
    substance = models.CharField(max_length=200)
    strength = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=12, choices=R.choices(R.STRENGTH_UNITS), blank=True)
    route = models.CharField(max_length=16, choices=R.choices(R.ROUTES), blank=True)
    schedule = models.JSONField(default=dict, blank=True)
    started = models.DateField(null=True, blank=True)
    stopped = models.DateField(null=True, blank=True)
    indication = models.TextField(blank=True)
    practitioner = models.ForeignKey(
        Practitioner, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    purpose_note = models.TextField(blank=True)

    class Meta(OwnedRecord.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(stopped__gte=F("started"))
                | Q(stopped__isnull=True)
                | Q(started__isnull=True),
                name="medication_stopped_after_started",
            )
        ]

    def clean(self):
        if self.schedule:
            self.schedule = R.validate_schedule(self.schedule)
        self.substance = self.substance.strip()
        if self.unit:
            try:
                self.unit = R.canonical_unit(self.unit)
            except ValidationError as exc:
                raise ValidationError({"unit": exc.messages[0]}) from exc
        others = Medication.objects.filter(
            user=self.user, substance__iexact=self.substance
        ).exclude(pk=self.pk)
        for other in others:
            if courses_overlap(self, other):
                raise ValidationError(
                    "Another course of this substance overlaps; close the current course the day "
                    "before the new one starts."
                )

    def __str__(self):
        return self.substance

    @property
    def is_active(self):
        return self.stopped is None


class Dose(OwnedRecord):
    medication = models.ForeignKey(Medication, on_delete=models.PROTECT, related_name="dose_events")
    status = models.CharField(max_length=8, choices=R.choices(R.DOSE_STATUS), default="taken")
    at = models.DateTimeField()
    scheduled_for = models.DateTimeField(null=True, blank=True)
    dose = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta(OwnedRecord.Meta):
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["user", "-at"]),
            models.Index(fields=["medication", "at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "medication", "scheduled_for"],
                condition=Q(scheduled_for__isnull=False),
                name="uniq_dose_per_slot",
            )
        ]

    def __str__(self):
        return f"{self.medication} {self.status}"


class Vaccination(OwnedRecord):
    vaccine = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16, choices=R.choices(R.VACCINATION_STATUS), default="completed"
    )
    dose_number = models.PositiveSmallIntegerField(null=True, blank=True)
    lot = models.CharField(max_length=100, blank=True)
    at = models.DateField(null=True, blank=True)
    site = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.vaccine


class Lab(OwnedRecord):
    """One analyte in a panel; exactly one of value and value_text."""

    panel = models.CharField(max_length=100)
    analyte = models.CharField(max_length=100)
    value = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    value_text = models.CharField(max_length=100, null=True, blank=True)
    unit = models.CharField(max_length=32, blank=True)
    ref_low = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    ref_high = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    fasting = models.BooleanField(null=True, blank=True)
    collected_at = models.DateTimeField()
    lab_name = models.CharField(max_length=200, blank=True)

    class Meta(OwnedRecord.Meta):
        verbose_name_plural = "labs"
        ordering = ["-collected_at"]
        indexes = [models.Index(fields=["user", "-collected_at"])]
        constraints = [
            models.CheckConstraint(
                condition=(Q(value__isnull=False) & Q(value_text__isnull=True))
                | (Q(value__isnull=True) & Q(value_text__isnull=False)),
                name="lab_value_xor_text",
            )
        ]

    def clean(self):
        if (self.value is None) == (self.value_text is None):
            raise ValidationError("A result needs a number or a text value, not both.")

    def __str__(self):
        return f"{self.panel} {self.analyte}"


class Measurement(OwnedRecord):
    metric = models.CharField(max_length=32)
    value = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=16, blank=True)
    at = models.DateTimeField()
    posture = models.CharField(max_length=8, choices=R.choices(R.POSTURE), blank=True)
    side = models.CharField(max_length=6, choices=R.choices(R.SIDE), blank=True)
    fasted = models.BooleanField(null=True, blank=True)
    device = models.CharField(max_length=100, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="components"
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta(OwnedRecord.Meta):
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["user", "-at"]),
            models.Index(fields=["user", "metric", "-at"]),
        ]

    def clean(self):
        metric = R.resolve_metric(self.metric)
        if not {"log", "panel"} & metric.use:
            raise ValidationError({"metric": "This metric cannot be measured."})
        self.metric = metric.code
        if "panel" in metric.use:
            # A panel carries no number of its own: its components hold the reading.
            if self.value is not None:
                raise ValidationError({"value": "A panel reading has no single value."})
            self.unit = ""
            return
        if self.value is not None:
            self.value = Decimal(self.value).quantize(Decimal(1).scaleb(-metric.digits))
        if self.unit and self.unit != metric.unit:
            raise ValidationError({"unit": "Unit does not match the metric."})
        self.unit = metric.unit
        if self.parent and self.parent.user_id != self.user_id:
            raise ValidationError({"parent": "Unknown measurement."})

    @property
    def is_panel(self):
        """A reading of two numbers, stored as this row plus one component per number."""
        metric = R.METRICS.get(self.metric)
        return metric is not None and "panel" in metric.use

    def components_by_metric(self):
        return {part.metric: part.value for part in self.components.all()}

    @property
    def reading(self):
        """`120/80` for a panel, the plain number otherwise; `—` when nothing is recorded."""
        if self.is_panel:
            parts = self.components_by_metric()
            numbers = [parts.get("systolic_bp"), parts.get("diastolic_bp")]
        else:
            numbers = [self.value]
        if any(number is None for number in numbers):
            return "—"
        return "/".join(_plain(number) for number in numbers)

    @property
    def unit_text(self):
        """`mmHg` for a panel, whose own unit column is empty; the stored unit otherwise."""
        if self.is_panel:
            unit = R.METRICS["systolic_bp"].unit
        else:
            unit = self.unit
        return R.UNIT_DISPLAY.get(unit, unit)

    def __str__(self):
        return f"{self.metric} {self.value if self.value is not None else ''}".strip()


class Symptom(OwnedRecord):
    name = models.CharField(max_length=200)
    severity = models.PositiveSmallIntegerField(null=True, blank=True)
    interference = models.CharField(
        max_length=8, choices=R.choices(R.SYMPTOM_INTERFERENCE), blank=True
    )
    onset_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    body_area = models.CharField(max_length=16, choices=R.choices(R.BODY_AREAS), blank=True)
    side = models.CharField(max_length=6, choices=R.choices(R.BODY_SIDE), default="na")
    trigger = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)

    class Meta(OwnedRecord.Meta):
        ordering = ["-onset_at"]
        indexes = [models.Index(fields=["user", "-onset_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(severity__gte=0, severity__lte=R.SYMPTOM_SEVERITY_MAX)
                | Q(severity__isnull=True),
                name="symptom_severity_range",
            ),
            models.CheckConstraint(
                condition=Q(resolved_at__gte=F("onset_at")) | Q(resolved_at__isnull=True),
                name="symptom_resolved_after_onset",
            ),
        ]

    def __str__(self):
        return self.name


class Checkin(OwnedRecord):
    at = models.DateTimeField()
    energy = models.PositiveSmallIntegerField(null=True, blank=True)
    mood = models.PositiveSmallIntegerField(null=True, blank=True)
    hunger = models.PositiveSmallIntegerField(null=True, blank=True)
    soreness = models.PositiveSmallIntegerField(null=True, blank=True)
    sleep_quality = models.PositiveSmallIntegerField(null=True, blank=True)
    stress = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta(OwnedRecord.Meta):
        ordering = ["-at"]
        indexes = [models.Index(fields=["user", "-at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(**{f"{field}__gte": 1, f"{field}__lte": 5})
                | models.Q(**{f"{field}__isnull": True}),
                name=f"checkin_{field}_range",
            )
            for field in R.CHECKIN_FIELDS
        ]

    def __str__(self):
        return f"Check-in {self.at:%Y-%m-%d}"


class Note(OwnedRecord):
    at = models.DateTimeField()
    body = models.TextField(validators=[MaxLengthValidator(5000)])
    tags = models.JSONField(default=list, blank=True)

    class Meta(OwnedRecord.Meta):
        ordering = ["-at"]
        indexes = [models.Index(fields=["user", "-at"])]

    def __str__(self):
        return self.body[:60]


class MemoryEntry(OwnedRecord):
    """One claim per row, the owner's wording; a changed claim is a new correction."""

    kind = models.CharField(max_length=14, choices=R.choices(R.MEMORY_KIND))
    subject = models.CharField(max_length=64)
    statement = models.CharField(max_length=280)
    observed_at = models.DateTimeField()
    source_ref = models.CharField(max_length=200, blank=True)
    importance = models.PositiveSmallIntegerField(default=3)
    status = models.CharField(max_length=10, choices=R.choices(R.MEMORY_STATUS), default="active")
    corrects = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrected_by"
    )
    valid_until = models.DateTimeField(null=True, blank=True)
    pinned = models.BooleanField(default=False)
    locked = models.BooleanField(default=False)
    due_at = models.DateTimeField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)

    class Meta(OwnedRecord.Meta):
        verbose_name_plural = "memory entries"
        ordering = ["-observed_at"]
        indexes = [
            models.Index(fields=["user", "status", "kind", "subject"]),
            models.Index(fields=["user", "pinned", "-observed_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(importance__gte=1, importance__lte=5), name="memory_importance_range"
            ),
            models.CheckConstraint(
                condition=Q(due_at__isnull=False) | ~Q(kind="follow_up"),
                name="memory_follow_up_has_due_at",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__gte=F("observed_at")) | Q(valid_until__isnull=True),
                name="memory_valid_until_after_observed",
            ),
        ]

    @property
    def recorded_at(self):
        """When the row was written; created_at never changes."""
        return self.created_at

    def clean(self):
        self.subject = R.normalise_subject(self.subject)
        if self.kind == "follow_up" and self.due_at is None:
            raise ValidationError({"due_at": "A follow-up needs a due date."})
        if self.kind == "correction" and not self.corrects_id:
            raise ValidationError({"kind": "A correction must correct an existing entry."})
        # These checks describe creating a correction; after it supersedes the old claim,
        # the predecessor's status is legitimately no longer active.
        if self.corrects_id and self._state.adding:
            if self.corrects.user_id != self.user_id:
                raise ValidationError("Unknown entry.")
            if self.corrects.status != "active":
                raise ValidationError("This claim is no longer active; correct its replacement.")
            # A missing or unparseable timestamp is a field error, not a crash.
            if self.observed_at and self.observed_at < self.corrects.observed_at:
                raise ValidationError(
                    {"observed_at": "A correction cannot be observed before the claim it corrects."}
                )

    def __str__(self):
        return self.statement


def supersede(old, correction):
    """One transaction: the correction replaces the claim and dates the old row out."""
    with transaction.atomic():
        old = (
            MemoryEntry._default_manager.filter(user_id=correction.user_id)
            .select_for_update()
            .get(pk=old.pk)
        )
        if old.locked:
            raise ValidationError("This entry is locked; unlock it before correcting it.")
        if old.status != "active":
            raise ValidationError("This claim is no longer active; correct its replacement.")
        correction.corrects = old
        correction.kind = "correction"
        correction.status = "active"
        correction.save()
        old.status = "superseded"
        old.valid_until = correction.observed_at
        old.save(update_fields=["status", "valid_until", "updated_at"])
    return correction


class Appointment(OwnedRecord):
    at = models.DateTimeField()
    practitioner = models.ForeignKey(
        Practitioner, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    practitioner_name = models.CharField(max_length=200, blank=True)
    specialty = models.CharField(max_length=100, blank=True)
    place = models.CharField(max_length=200, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=10, choices=R.choices(R.APPOINTMENT_STATUS), default="planned"
    )
    outcome_note = models.TextField(blank=True)

    class Meta(OwnedRecord.Meta):
        ordering = ["at"]
        indexes = [models.Index(fields=["user", "at"])]

    def __str__(self):
        return f"{self.practitioner_name or self.place or 'Appointment'} {self.at:%Y-%m-%d}"


def dates_overlap(first_start, first_stop, second_start, second_stop):
    """Two inclusive date ranges share at least one day; None stops mean ongoing."""
    return (first_start or date.min) <= (second_stop or date.max) and (
        second_start or date.min
    ) <= (first_stop or date.max)


def courses_overlap(first, second):
    """Two courses of one substance must not share a day."""
    return dates_overlap(first.started, first.stopped, second.started, second.stopped)


DOMAIN_MODELS = {
    "identity": Person,
    "environment": Environment,
    "goals": Goal,
    "program": Program,
    "conditions": Condition,
    "injuries": Injury,
    "allergies": Allergy,
    "medications": Medication,
    "measurements": Measurement,
    "symptoms": Symptom,
    "checkins": Checkin,
    "vaccinations": Vaccination,
    "appointments": Appointment,
    "notes": Note,
    "labs": Lab,
    "memory": MemoryEntry,
}


def domain_state(user, domain):
    """present iff a row exists; none only when set explicitly; else unknown."""
    model = DOMAIN_MODELS.get(domain)
    if model is not None and model._default_manager.filter(user=user).exists():
        return "present"
    explicit = (
        DomainState.objects.filter(user=user, domain=domain).values_list("state", flat=True).first()
    )
    return explicit if explicit == "none" else "unknown"
