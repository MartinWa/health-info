"""Owner-scoped forms: labels above inputs, decimal keypads, no echoed values."""

from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from records import models as M
from records import registries as R


class LocalDateTimeField(forms.DateTimeField):
    input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"]


class LocalDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, attrs=None):
        super().__init__(attrs, format="%Y-%m-%dT%H:%M")


class OwnerForm(forms.ModelForm):
    """Every form knows its owner, so related choices never cross accounts."""

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        # Validation runs model clean() before save(), so the owner must already be set.
        if hasattr(self.instance, "user_id") and not self.instance.user_id:
            self.instance.user = user
        for field in self.fields.values():
            widget = field.widget
            if isinstance(field, LocalDateTimeField):
                widget = LocalDateTimeInput()
                field.widget = widget
            widget.attrs.setdefault("class", "input")
            if isinstance(field, (forms.DecimalField, forms.FloatField)):
                widget.attrs.setdefault("inputmode", "decimal")
            elif isinstance(field, forms.IntegerField):
                widget.attrs.setdefault("inputmode", "numeric")
            if isinstance(field, (forms.DateField, forms.DateTimeField)):
                widget.attrs.setdefault("autocomplete", "off")
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "checkbox")
        if "practitioner" in self.fields:
            self.fields["practitioner"].queryset = M.Practitioner.objects.filter(user=user)
        if "at" in self.fields and not self.is_bound and not self.instance.pk:
            self.initial["at"] = timezone.localtime().replace(second=0, microsecond=0)
        for name in ("start", "at"):
            field = self.fields.get(name)
            if field is None or self.is_bound or self.instance.pk:
                continue
            if isinstance(field, forms.DateField) and not isinstance(field, forms.DateTimeField):
                self.initial.setdefault(name, timezone.localdate())
        if "onset_at" in self.fields and not self.is_bound and not self.instance.pk:
            self.initial["onset_at"] = timezone.localtime().replace(second=0, microsecond=0)

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.user = self.user
        if commit:
            instance.save()
        return instance


class PersonForm(OwnerForm):
    class Meta:
        model = M.Person
        fields = [
            "name",
            "birth_date",
            "birth_date_precision",
            "sex_at_birth",
            "gender",
            "height_cm",
            "timezone",
            "unit_system",
            "locale",
        ]
        help_texts = {"timezone": "IANA name, e.g. Europe/Stockholm."}


class EnvironmentForm(OwnerForm):
    equipment = forms.MultipleChoiceField(
        choices=R.choices(R.EQUIPMENT), widget=forms.CheckboxSelectMultiple, required=False
    )

    class Meta:
        model = M.Environment
        fields = [
            "country",
            "region",
            "urban_rural",
            "gym_access",
            "equipment",
            "work_pattern",
            "work_load",
            "cooks_for",
            "cooking_facilities",
            "tobacco",
            "tobacco_amount",
            "alcohol",
            "alcohol_units",
            "notes",
        ]


class GoalForm(OwnerForm):
    metric = forms.ChoiceField(
        choices=[
            (code, metric.display) for code, metric in R.METRICS.items() if "goal" in metric.use
        ]
    )

    class Meta:
        model = M.Goal
        fields = [
            "metric",
            "direction",
            "target",
            "target_high",
            "by",
            "review_at",
            "priority",
            "status",
            "baseline_value",
            "baseline_on",
            "note",
        ]

    def clean_metric(self):
        return R.resolve_metric(self.cleaned_data["metric"]).code

    @property
    def warnings(self):
        """Conflicts warn, never block."""
        if not self.is_valid() or self.cleaned_data.get("status") != "active":
            return []
        metric = self.cleaned_data["metric"]
        direction = self.cleaned_data["direction"]
        others = M.Goal.objects.filter(user=self.user, metric=metric, status="active")
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        warnings = []
        for other in others:
            if other.direction != direction:
                warnings.append("Another active goal on this metric has the opposite direction.")
                break
        if R.METRICS[metric].authority.startswith("external:"):
            warnings.append(
                "This metric is recorded outside health-info; the goal cannot track it alone."
            )
        return warnings


class ProgramForm(OwnerForm):
    workout_types = forms.CharField(
        required=False, help_text="Comma separated, e.g. strength_training, running."
    )

    class Meta:
        model = M.Program
        fields = [
            "name",
            "phase",
            "started",
            "ended",
            "sessions_per_week",
            "deload_every_weeks",
            "progression",
            "workout_types",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["workout_types"] = ", ".join(self.instance.workout_types)

    def clean_workout_types(self):
        value = self.cleaned_data["workout_types"]
        return [R.normalise_workout_type(part) for part in value.split(",") if part.strip()]


class PractitionerForm(OwnerForm):
    class Meta:
        model = M.Practitioner
        fields = ["name", "specialty", "organisation", "contact"]


class ConditionForm(OwnerForm):
    class Meta:
        model = M.Condition
        fields = [
            "name",
            "code",
            "since",
            "since_precision",
            "status",
            "verification_status",
            "severity",
            "abatement",
            "note",
            "practitioner",
        ]


class InjuryForm(OwnerForm):
    limits = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One per line: avoid|caution pattern [exercise]. Patterns are in §A.5.",
    )

    class Meta:
        model = M.Injury
        fields = [
            "area",
            "side",
            "since",
            "since_precision",
            "status",
            "verification_status",
            "mechanism",
            "rehab",
            "limits",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["limits"] = "\n".join(
                " ".join(
                    part
                    for part in (limit.get("kind"), limit.get("pattern"), limit.get("exercise"))
                    if part
                )
                for limit in self.instance.limits
            )

    def clean_limits(self):
        limits = []
        for line in self.cleaned_data["limits"].splitlines():
            if not line.strip():
                continue
            parts = line.split(maxsplit=2)
            if len(parts) < 2:
                raise ValidationError("Each line needs a kind and a pattern.")
            kind, pattern = parts[0].lower(), parts[1].lower()
            if kind not in R.LIMIT_KIND:
                raise ValidationError("Each line starts with avoid or caution.")
            if pattern not in R.MOVEMENT_PATTERNS:
                raise ValidationError("Unknown movement pattern.")
            limit = {"pattern": pattern, "kind": kind}
            if len(parts) == 3 and parts[2].strip():
                limit["exercise"] = parts[2].strip()
            limits.append(limit)
        return limits


class AllergyForm(OwnerForm):
    class Meta:
        model = M.Allergy
        fields = ["substance", "type", "reaction", "severity", "criticality", "note"]


class MedicationForm(OwnerForm):
    schedule_type = forms.ChoiceField(choices=R.choices(R.SCHEDULE_TYPE))
    times = forms.CharField(
        required=False, help_text="24-hour HH:MM, comma separated. Empty means time unknown."
    )
    every_days = forms.IntegerField(required=False, min_value=1, max_value=365)
    days_of_week = forms.CharField(required=False, help_text="ISO weekdays, e.g. 1,4")
    on_days = forms.IntegerField(required=False, min_value=1, max_value=365)
    off_days = forms.IntegerField(required=False, min_value=0, max_value=365)
    max_per_day = forms.IntegerField(required=False, min_value=1, max_value=24)
    with_food = forms.ChoiceField(
        choices=[("", "Not recorded")] + R.choices(R.WITH_FOOD), required=False
    )
    change_date = forms.DateField(
        required=False,
        help_text="Required when strength or schedule changes on an ongoing course.",
    )
    stopped = forms.DateField(required=False, help_text="Leave empty while the course is ongoing.")

    class Meta:
        model = M.Medication
        fields = [
            "kind",
            "substance",
            "strength",
            "unit",
            "route",
            "started",
            "stopped",
            "indication",
            "practitioner",
            "purpose_note",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            schedule = self.instance.schedule or {}
            self.initial.setdefault("schedule_type", schedule.get("type", "daily"))
            self.initial.setdefault("times", ", ".join(schedule.get("times", [])))
            self.initial.setdefault("every_days", schedule.get("every_days"))
            self.initial.setdefault(
                "days_of_week", ",".join(str(day) for day in schedule.get("days_of_week", []))
            )
            self.initial.setdefault("on_days", schedule.get("on_days"))
            self.initial.setdefault("off_days", schedule.get("off_days"))
            self.initial.setdefault("max_per_day", schedule.get("max_per_day"))
            self.initial.setdefault("with_food", schedule.get("with_food", ""))

    def build_schedule(self):
        data = self.cleaned_data
        schedule = {"type": data.get("schedule_type")}
        if data.get("times"):
            schedule["times"] = [part.strip() for part in data["times"].split(",") if part.strip()]
        if data["schedule_type"] == "interval":
            schedule["every_days"] = data.get("every_days")
        if data["schedule_type"] == "weekly":
            days = [
                part.strip() for part in (data.get("days_of_week") or "").split(",") if part.strip()
            ]
            schedule["days_of_week"] = [int(day) if day.isdigit() else day for day in days]
        if data["schedule_type"] == "cycle":
            schedule["on_days"] = data.get("on_days")
            schedule["off_days"] = data.get("off_days")
        if data["schedule_type"] == "prn":
            schedule["max_per_day"] = data.get("max_per_day")
        if data.get("with_food"):
            schedule["with_food"] = data["with_food"]
        return R.validate_schedule(schedule)

    def clean(self):
        cleaned = super().clean()
        if "schedule_type" not in cleaned:
            return cleaned
        if not cleaned.get("substance"):
            return cleaned
        try:
            schedule = self.build_schedule()
        except ValidationError as exc:
            self.add_error("schedule_type", exc.messages[0])
            return cleaned
        previous_schedule = self.instance.schedule if self.instance.pk else {}
        previous_strength = self.instance.strength if self.instance.pk else None
        # `schedule` is not a form field, so _post_clean leaves this value in place.
        self.instance.schedule = schedule
        started = cleaned.get("started") or (self.instance.started if self.instance.pk else None)
        stopped = cleaned.get("stopped")
        if stopped and started and stopped < started:
            self.add_error("stopped", "A course cannot stop before it starts.")
        changed = previous_strength != cleaned.get("strength") or previous_schedule != schedule
        self.split_course = False
        if self.instance.pk and self.instance.stopped is None and changed:
            change_date = cleaned.get("change_date")
            if not change_date:
                self.add_error(
                    "change_date",
                    "This ends the current course and starts a new one; enter the change date.",
                )
            elif started and change_date <= started:
                self.add_error("change_date", "The change date must be after the course start.")
            elif stopped and stopped <= change_date:
                self.add_error("stopped", "A course change must come before the stop date.")
            else:
                self.split_course = True
        if self.split_course:
            stop = cleaned["change_date"] - timedelta(days=1)
            new_started = cleaned["change_date"]
        else:
            stop = stopped or self.instance.stopped
            new_started = started
        for other in M.Medication.objects.filter(
            user=self.user, substance__iexact=cleaned["substance"]
        ).exclude(pk=self.instance.pk):
            if M.dates_overlap(new_started, stop, other.started, other.stopped):
                raise ValidationError(
                    "Another course of this substance overlaps; close the current course the day "
                    "before the new one starts."
                )
        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        if getattr(self, "split_course", False):
            change_date = self.cleaned_data["change_date"]
            new = super().save(commit=False)
            old = M.Medication.objects.select_for_update().get(pk=self.instance.pk)
            old.stopped = change_date - timedelta(days=1)
            old.save()
            new.pk = None
            new._state.adding = True
            new.started = change_date
            new.stopped = self.cleaned_data.get("stopped")
            new.save()
            return new
        return super().save(commit=commit)


class VaccinationForm(OwnerForm):
    class Meta:
        model = M.Vaccination
        fields = ["vaccine", "status", "dose_number", "lot", "at", "site"]

    @property
    def warnings(self):
        at = self.cleaned_data.get("at") if self.is_valid() else None
        if self.cleaned_data.get("status") == "entered_in_error" and at:
            if at < timezone.localdate() - timedelta(days=30):
                return ["An entered-in-error vaccination older than 30 days; check the record."]
        return []


class AppointmentForm(OwnerForm):
    class Meta:
        model = M.Appointment
        fields = [
            "at",
            "practitioner",
            "practitioner_name",
            "specialty",
            "place",
            "reason",
            "status",
            "outcome_note",
        ]


class PeriodForm(OwnerForm):
    class Meta:
        model = M.Period
        fields = ["kind", "start", "end", "note"]


class LabForm(OwnerForm):
    """One analyte row, for editing a saved result."""

    class Meta:
        model = M.Lab
        fields = [
            "panel",
            "analyte",
            "value",
            "value_text",
            "unit",
            "ref_low",
            "ref_high",
            "fasting",
            "collected_at",
            "lab_name",
        ]
        # The panel is every row sharing a name and a collection time, so editing the time
        # moves this row out of its panel.
        help_texts = {"collected_at": "The panel is the rows with this name and collection time."}


class LabPanelForm(forms.Form):
    """Panel-level fields of a new panel; the analyte rows come from a formset."""

    panel = forms.CharField(max_length=100)
    collected_at = LocalDateTimeField(label="Collected at")
    lab_name = forms.CharField(max_length=200, required=False, label="Lab")
    fasting = forms.NullBooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
        if not self.is_bound:
            self.initial.setdefault(
                "collected_at", timezone.localtime().replace(second=0, microsecond=0)
            )
        self.fields["collected_at"].widget = LocalDateTimeInput(attrs={"class": "input"})


class LabAnalyteForm(forms.ModelForm):
    """One analyte row; the view supplies owner, panel and collected_at."""

    class Meta:
        model = M.Lab
        fields = ["analyte", "value", "value_text", "unit", "ref_low", "ref_high"]
        labels = {"value_text": "Value (text)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
            if isinstance(field, forms.DecimalField):
                field.widget.attrs.setdefault("inputmode", "decimal")


class MeasurementForm(OwnerForm):
    """One metric per row; blood pressure has its own paired form."""

    metric = forms.ChoiceField(
        choices=[
            (code, metric.display)
            for code, metric in R.METRICS.items()
            if "log" in metric.use and not metric.paired
        ]
    )

    class Meta:
        model = M.Measurement
        fields = ["metric", "value", "at", "posture", "side", "fasted", "device", "note"]

    def clean_metric(self):
        return R.resolve_metric(self.cleaned_data["metric"]).code

    def clean(self):
        cleaned = super().clean()
        metric = R.METRICS.get(cleaned.get("metric"))
        value = cleaned.get("value")
        self.warnings = R.check_value(metric, value) if metric and value is not None else []
        return cleaned


class BloodPressureForm(forms.Form):
    systolic = forms.IntegerField(
        min_value=1,
        label="Systolic",
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "class": "input big"}),
    )
    diastolic = forms.IntegerField(
        min_value=1,
        label="Diastolic",
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "class": "input big"}),
    )
    pulse = forms.IntegerField(
        min_value=1,
        required=False,
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "class": "input big"}),
    )
    at = LocalDateTimeField(label="Measured at")
    note = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
        if not self.is_bound:
            self.initial["at"] = timezone.localtime().replace(second=0, microsecond=0)
        self.fields["at"].widget = LocalDateTimeInput(attrs={"class": "input"})

    def clean(self):
        cleaned = super().clean()
        self.warnings = []
        for name in ("systolic", "diastolic", "pulse"):
            metric = R.METRICS[
                {"systolic": "systolic_bp", "diastolic": "diastolic_bp", "pulse": "pulse"}[name]
            ]
            if cleaned.get(name) is not None:
                self.warnings.extend(R.check_value(metric, cleaned[name]))
        if (cleaned.get("systolic") is not None) != (cleaned.get("diastolic") is not None):
            raise ValidationError("A blood pressure reading needs both numbers.")
        return cleaned

    @transaction.atomic
    def save(self):
        at = self.cleaned_data["at"]
        note = self.cleaned_data.get("note") or ""
        panel = M.Measurement.objects.create(
            user=self.user, metric="blood_pressure", at=at, note=note
        )
        for code in ("systolic_bp", "diastolic_bp"):
            metric = R.METRICS[code]
            M.Measurement.objects.create(
                user=self.user,
                metric=code,
                value=self.cleaned_data["systolic" if code == "systolic_bp" else "diastolic"],
                unit=metric.unit,
                at=at,
                parent=panel,
            )
        if self.cleaned_data.get("pulse") is not None:
            M.Measurement.objects.create(
                user=self.user,
                metric="pulse",
                value=self.cleaned_data["pulse"],
                unit=R.METRICS["pulse"].unit,
                at=at,
            )
        return panel


class SymptomForm(OwnerForm):
    severity = forms.TypedChoiceField(
        choices=[(value, str(value)) for value in range(R.SYMPTOM_SEVERITY_MAX + 1)],
        coerce=int,
        widget=forms.RadioSelect,
        empty_value=None,
        required=False,
    )
    onset_at = LocalDateTimeField()
    resolved_at = LocalDateTimeField(required=False)

    class Meta:
        model = M.Symptom
        fields = [
            "name",
            "severity",
            "interference",
            "onset_at",
            "resolved_at",
            "body_area",
            "side",
            "trigger",
            "note",
        ]


class CheckinForm(OwnerForm):
    at = LocalDateTimeField()

    class Meta:
        model = M.Checkin
        fields = ["at", *R.CHECKIN_FIELDS, "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in R.CHECKIN_FIELDS:
            low, high = R.CHECKIN_ANCHORS[name]
            self.fields[name] = forms.TypedChoiceField(
                choices=[(value, str(value)) for value in range(1, 6)],
                coerce=int,
                required=False,
                widget=forms.RadioSelect,
                label=f"{name.replace('_', ' ').capitalize()} (1 {low}, 5 {high})",
            )


class MemoryForm(OwnerForm):
    observed_at = LocalDateTimeField()
    due_at = LocalDateTimeField(required=False)
    tags = forms.CharField(required=False, help_text="Comma separated.")

    class Meta:
        model = M.MemoryEntry
        fields = [
            "kind",
            "subject",
            "statement",
            "observed_at",
            "importance",
            "source_ref",
            "tags",
            "pinned",
            "due_at",
        ]
        help_texts = {
            "subject": "One slug, e.g. training, sleep, medication.",
            "statement": "One claim, in your own words. 280 characters.",
            "observed_at": "When this was true, not when you typed it.",
            "source_ref": "Where it came from: a conversation, a report, a device.",
            "due_at": "Required for a follow-up.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["tags"] = ", ".join(self.instance.tags)

    def clean_tags(self):
        return [part.strip() for part in self.cleaned_data["tags"].split(",") if part.strip()]


class MemoryEditForm(MemoryForm):
    """Typos, tags and pinning; a changed claim is a correction, not an edit."""

    # Declared on MemoryForm; an in-place edit never moves the observation time.
    observed_at = None

    class Meta(MemoryForm.Meta):
        fields = ["subject", "statement", "tags", "pinned", "due_at"]


class MemoryCorrectionForm(MemoryForm):
    """A new row that replaces the old claim; the server writes the supersede chain."""

    class Meta(MemoryForm.Meta):
        fields = ["subject", "statement", "observed_at", "importance", "source_ref", "tags"]

    def save(self, commit=True):
        old = M.MemoryEntry._default_manager.get(pk=self.instance.corrects_id, user_id=self.user.pk)
        self.instance = super().save(commit=False)
        return M.supersede(old, self.instance)


class TokenForm(forms.Form):
    name = forms.CharField(max_length=100, help_text="Where this token is used, e.g. health-mcp.")
    expires_at = forms.DateTimeField(
        required=False,
        label="Expires at",
        widget=LocalDateTimeInput(attrs={"class": "input"}),
        help_text="Leave empty for no expiry.",
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.setdefault("class", "input")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if M.ApiToken._default_manager.filter(
            user=self.user, name=name, revoked_at__isnull=True
        ).exists():
            raise ValidationError("An active token already has this name.")
        return name


class NoteForm(OwnerForm):
    tags = forms.CharField(required=False, help_text="Comma separated.")

    class Meta:
        model = M.Note
        fields = ["at", "body", "tags"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["tags"] = ", ".join(self.instance.tags)

    def clean_tags(self):
        return [part.strip() for part in self.cleaned_data["tags"].split(",") if part.strip()]
