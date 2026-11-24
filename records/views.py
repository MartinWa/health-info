"""Server-rendered UI: GET renders, POST validates and redirects (303)."""

import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from io import BytesIO
from urllib.parse import urlencode

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count, Min, ProtectedError, Q
from django.forms import formset_factory
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from records import forms as F
from records import lockout, summary
from records import models as M
from records import registries as R
from records import schedule as S


def see_other(url):
    return HttpResponse(status=303, headers={"Location": url})


def audit(user, action, kind, object_id="", detail=None):
    M.AuditEvent._default_manager.create(
        user=user, action=action, object_kind=kind, object_id=str(object_id), detail=detail or {}
    )


def display(obj, name):
    """Human text for one field, without ever defaulting to a raw code."""
    field = obj._meta.get_field(name)
    value = getattr(obj, name)
    if value is None or value == "":
        return "—"
    if name == "metric" and isinstance(value, str):
        try:
            return R.resolve_metric(value).display
        except ValidationError:
            return value
    if name == "unit" and value in R.UNIT_DISPLAY:
        return R.UNIT_DISPLAY[value]
    if getattr(field, "choices", None) and isinstance(value, str):
        return dict(field.choices).get(value, value)
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict) and "type" in value:
        return S.describe_schedule(value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {val}" for key, val in value.items()) or "—"
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return "; ".join(_limit_text(item) for item in value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    return value


def _limit_text(limit):
    text = " ".join(part for part in (limit.get("kind"), limit.get("pattern")) if part)
    if limit.get("exercise"):
        text += f" ({limit['exercise']})"
    return text


def healthz(request):
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})


# --- Generic owner-scoped views --------------------------------------------------------


class OwnedMixin:
    model = None
    queryset_filter = {}

    def get_queryset(self):
        return self.model._default_manager.filter(user=self.request.user, **self.queryset_filter)


class RecordList(LoginRequiredMixin, OwnedMixin, ListView):
    template_name = "records/list.html"
    paginate_by = 50
    title = ""
    domain = None
    columns = ()
    detail_url_name = None
    new_url_name = None
    empty_text = "Nothing recorded yet."

    def get_queryset(self):
        queryset = super().get_queryset()
        source = self.request.GET.get("source")
        if source in R.SOURCE:
            queryset = queryset.filter(source=source)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            title=self.title,
            domain=self.domain,
            column_labels=[label for label, _ in self.columns],
            rows=[
                {
                    "cells": [display(obj, name) for _, name in self.columns],
                    "source": obj.source,
                    "reported_by": obj.reported_by,
                    "detail_url": reverse(self.detail_url_name, args=[obj.pk])
                    if self.detail_url_name
                    else None,
                }
                for obj in context["object_list"]
            ],
            new_url=reverse(self.new_url_name) if self.new_url_name else None,
            empty_text=self.empty_text,
            state=M.domain_state(self.request.user, self.domain) if self.domain else None,
            source_filter=self.request.GET.get("source", ""),
        )
        return context


class RecordDetail(LoginRequiredMixin, OwnedMixin, DetailView):
    template_name = "records/detail.html"
    title = ""
    detail_fields = ()
    edit_url_name = None
    delete_url_name = None
    exclude_metrics = frozenset()
    detail_actions = None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            title=self.title,
            rows=[
                (name.replace("_", " ").capitalize(), display(self.object, name))
                for name in self.detail_fields
            ],
            edit_url=self._edit_url(),
            delete_url=reverse(self.delete_url_name, args=[self.object.pk])
            if self.delete_url_name
            else None,
            source=self.object.source,
            reported_by=self.object.reported_by,
            detail_actions=self.detail_actions(self.object) if self.detail_actions else [],
        )
        components = getattr(self.object, "components", None)
        if components is not None and components.exists():
            context["rows"] += [
                (display(component, "metric"), f"{display(component, 'value')} {component.unit}")
                for component in components.all()
            ]
        return context

    def _edit_url(self):
        if not self.edit_url_name:
            return None
        if getattr(self.object, "metric", None) in self.exclude_metrics:
            return None
        return reverse(self.edit_url_name, args=[self.object.pk])


class RecordCreate(LoginRequiredMixin, CreateView):
    template_name = "records/form.html"
    title = ""
    success_url_name = ""
    cancel_url_name = "records:dashboard"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            title=self.title, submit_label="Save", cancel_url=reverse(self.cancel_url_name)
        )
        return context

    def get_success_url(self):
        return reverse(self.success_url_name, args=[self.object.pk])

    def form_valid(self, form):
        self.object = form.save()
        audit(self.request.user, "create", self.object._meta.label, self.object.pk)
        notify_warnings(self.request, form)
        messages.success(self.request, "Saved.")
        return HttpResponseRedirect(self.get_success_url(), status=303)


class RecordUpdate(LoginRequiredMixin, OwnedMixin, UpdateView):
    template_name = "records/form.html"
    title = ""
    success_url_name = ""
    exclude_metrics = frozenset()

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.exclude_metrics:
            queryset = queryset.exclude(metric__in=self.exclude_metrics)
        return queryset

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            title=self.title,
            submit_label="Save",
            cancel_url=reverse(self.success_url_name, args=[self.object.pk]),
        )
        return context

    def get_success_url(self):
        return reverse(self.success_url_name, args=[self.object.pk])

    def form_valid(self, form):
        self.object = form.save()
        audit(self.request.user, "update", self.object._meta.label, self.object.pk)
        notify_warnings(self.request, form)
        messages.success(self.request, "Saved.")
        return HttpResponseRedirect(self.get_success_url(), status=303)


class RecordDelete(LoginRequiredMixin, OwnedMixin, DeleteView):
    template_name = "records/delete.html"
    title = ""
    success_url_name = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(title=self.title)
        return context

    def get_success_url(self):
        return reverse(self.success_url_name)

    def form_valid(self, form):
        try:
            self.object.delete()
        except ProtectedError:
            messages.error(
                self.request, "This record is referenced by other rows; remove those first."
            )
            return HttpResponseRedirect(self.get_success_url())
        audit(self.request.user, "delete", self.object._meta.label, self.object.pk)
        messages.success(self.request, "Deleted.")
        return HttpResponseRedirect(self.get_success_url(), status=303)


def notify_warnings(request, form):
    for warning in getattr(form, "warnings", []) or []:
        messages.warning(request, warning)


# --- Resource table: one place ties model, form, columns and routes --------------------


def symptom_actions(symptom):
    """Promote a symptom into the clinical record, with onset and area prefilled."""
    return [
        (label, reverse("records:promote_symptom", args=[symptom.pk, target]))
        for label, target in (("Make an injury", "injury"), ("Make a condition", "condition"))
    ]


RESOURCES = {
    "goals": {
        "model": M.Goal,
        "form": F.GoalForm,
        "title": "Goals",
        "domain": "goals",
        "columns": [
            ("Metric", "metric"),
            ("Direction", "direction"),
            ("Target", "target"),
            ("Status", "status"),
        ],
        "detail_fields": [
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
            "source",
            "reported_by",
        ],
    },
    "conditions": {
        "model": M.Condition,
        "form": F.ConditionForm,
        "title": "Conditions",
        "domain": "conditions",
        "columns": [
            ("Name", "name"),
            ("Since", "since"),
            ("Status", "status"),
            ("Verified", "verification_status"),
        ],
        "detail_fields": [
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
            "source",
            "reported_by",
        ],
    },
    "injuries": {
        "model": M.Injury,
        "form": F.InjuryForm,
        "title": "Injuries",
        "domain": "injuries",
        "columns": [
            ("Area", "area"),
            ("Side", "side"),
            ("Status", "status"),
            ("Verified", "verification_status"),
        ],
        "detail_fields": [
            "area",
            "side",
            "since",
            "since_precision",
            "status",
            "verification_status",
            "mechanism",
            "rehab",
            "limits",
            "source",
            "reported_by",
        ],
    },
    "allergies": {
        "model": M.Allergy,
        "form": F.AllergyForm,
        "title": "Allergies",
        "domain": "allergies",
        "columns": [("Substance", "substance"), ("Type", "type"), ("Severity", "severity")],
        "detail_fields": [
            "substance",
            "type",
            "reaction",
            "severity",
            "criticality",
            "note",
            "source",
            "reported_by",
        ],
    },
    "medications": {
        "model": M.Medication,
        "form": F.MedicationForm,
        "title": "Medications",
        "domain": "medications",
        "columns": [
            ("Substance", "substance"),
            ("Strength", "strength"),
            ("Unit", "unit"),
            ("Route", "route"),
            ("Started", "started"),
            ("Stopped", "stopped"),
        ],
        "detail_fields": [
            "kind",
            "substance",
            "strength",
            "unit",
            "route",
            "schedule",
            "started",
            "stopped",
            "indication",
            "practitioner",
            "purpose_note",
            "source",
            "reported_by",
        ],
    },
    "vaccinations": {
        "model": M.Vaccination,
        "form": F.VaccinationForm,
        "title": "Vaccinations",
        "domain": "vaccinations",
        "columns": [("Vaccine", "vaccine"), ("Status", "status"), ("At", "at")],
        "detail_fields": [
            "vaccine",
            "status",
            "dose_number",
            "lot",
            "at",
            "site",
            "source",
            "reported_by",
        ],
    },
    "practitioners": {
        "model": M.Practitioner,
        "form": F.PractitionerForm,
        "title": "Practitioners",
        "domain": None,
        "columns": [("Name", "name"), ("Specialty", "specialty"), ("Organisation", "organisation")],
        "detail_fields": ["name", "specialty", "organisation", "contact"],
    },
    "appointments": {
        "model": M.Appointment,
        "form": F.AppointmentForm,
        "title": "Appointments",
        "domain": "appointments",
        "columns": [("At", "at"), ("Practitioner", "practitioner_name"), ("Status", "status")],
        "detail_fields": [
            "at",
            "practitioner",
            "practitioner_name",
            "specialty",
            "place",
            "reason",
            "status",
            "outcome_note",
            "source",
            "reported_by",
        ],
    },
    "periods": {
        "model": M.Period,
        "form": F.PeriodForm,
        "title": "Periods",
        "domain": None,
        "columns": [("Kind", "kind"), ("Start", "start"), ("End", "end")],
        "detail_fields": ["kind", "start", "end", "note", "source", "reported_by"],
    },
    "measurements": {
        "model": M.Measurement,
        "form": F.MeasurementForm,
        "user_filter": {"parent__isnull": True},
        "exclude_metrics": {"blood_pressure"},
        "title": "Measurements",
        "domain": "measurements",
        "columns": [("At", "at"), ("Metric", "metric"), ("Value", "value"), ("Unit", "unit")],
        "detail_fields": [
            "metric",
            "value",
            "unit",
            "at",
            "posture",
            "side",
            "fasted",
            "device",
            "note",
        ],
    },
    "symptoms": {
        "model": M.Symptom,
        "form": F.SymptomForm,
        "title": "Symptoms",
        "domain": "symptoms",
        "columns": [
            ("Name", "name"),
            ("Onset", "onset_at"),
            ("Severity", "severity"),
            ("Area", "body_area"),
        ],
        "detail_fields": [
            "name",
            "severity",
            "interference",
            "onset_at",
            "resolved_at",
            "body_area",
            "side",
            "trigger",
            "note",
            "source",
            "reported_by",
        ],
        "detail_actions": symptom_actions,
    },
    "checkins": {
        "model": M.Checkin,
        "form": F.CheckinForm,
        "title": "Check-ins",
        "domain": "checkins",
        "columns": [
            ("At", "at"),
            ("Energy", "energy"),
            ("Mood", "mood"),
            ("Sleep", "sleep_quality"),
        ],
        "detail_fields": ["at", *R.CHECKIN_FIELDS, "note"],
    },
    "notes": {
        "model": M.Note,
        "form": F.NoteForm,
        "title": "Notes",
        "domain": "notes",
        "columns": [("At", "at"), ("Note", "body")],
        "detail_fields": ["at", "body", "tags", "source", "reported_by"],
    },
}


SINGULAR = {
    "goals": "goal",
    "conditions": "condition",
    "injuries": "injury",
    "allergies": "allergy",
    "medications": "medication course",
    "vaccinations": "vaccination",
    "practitioners": "practitioner",
    "appointments": "appointment",
    "periods": "period",
    "measurements": "measurement",
    "symptoms": "symptom",
    "checkins": "check-in",
    "notes": "note",
}


def resource_views(slug, config):
    """Build the five owner-scoped views for one resource from its single config entry."""
    names = f"records:{slug}"
    singular = SINGULAR[slug]
    list_view = type(
        f"{slug}_list",
        (RecordList,),
        {
            "model": config["model"],
            "title": config["title"],
            "domain": config["domain"],
            "columns": config["columns"],
            "queryset_filter": config.get("user_filter", {}),
            "new_url_name": f"{names}_new",
            "detail_url_name": f"{names}_detail",
        },
    ).as_view()
    detail_view = type(
        f"{slug}_detail",
        (RecordDetail,),
        {
            "model": config["model"],
            "title": SINGULAR[slug].capitalize(),
            "detail_fields": config["detail_fields"],
            "queryset_filter": config.get("user_filter", {}),
            "exclude_metrics": config.get("exclude_metrics", frozenset()),
            "edit_url_name": f"{names}_edit",
            "delete_url_name": f"{names}_delete",
            "detail_actions": staticmethod(config["detail_actions"])
            if config.get("detail_actions")
            else None,
        },
    ).as_view()
    create_view = type(
        f"{slug}_new",
        (RecordCreate,),
        {
            "model": config["model"],
            "form_class": config["form"],
            "title": f"Add {singular}",
            "success_url_name": f"{names}_detail",
            "cancel_url_name": f"{names}_list",
        },
    ).as_view()
    update_view = type(
        f"{slug}_edit",
        (RecordUpdate,),
        {
            "model": config["model"],
            "form_class": config["form"],
            "title": f"Edit {singular}",
            "success_url_name": f"{names}_detail",
            "queryset_filter": config.get("user_filter", {}),
            "exclude_metrics": config.get("exclude_metrics", frozenset()),
        },
    ).as_view()
    delete_view = type(
        f"{slug}_delete",
        (RecordDelete,),
        {
            "model": config["model"],
            "title": f"Delete {singular}",
            "success_url_name": f"{names}_list",
            "queryset_filter": config.get("user_filter", {}),
        },
    ).as_view()
    return {
        "list": list_view,
        "detail": detail_view,
        "new": create_view,
        "edit": update_view,
        "delete": delete_view,
    }


RESOURCE_VIEWS = {slug: resource_views(slug, config) for slug, config in RESOURCES.items()}


# --- Labs: one panel page per panel, analytes added in a single form --------------------


def _lab_group(user, row):
    """A panel is the rows sharing a name and a collection time."""
    return M.Lab._default_manager.filter(user=user, panel=row.panel, collected_at=row.collected_at)


def _reference(lab):
    low, high = lab.ref_low, lab.ref_high
    if low is not None and high is not None:
        return f"{display(lab, 'ref_low')}–{display(lab, 'ref_high')}"
    if high is not None:
        return f"<{display(lab, 'ref_high')}"
    return f">{display(lab, 'ref_low')}" if low is not None else ""


def _when(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "\u2014"


def _previous_lab_panel(user, panel):
    """The newest saved panel with this name, ordered by analyte."""
    if not panel:
        return []
    newest = (
        M.Lab._default_manager.filter(user=user, panel=panel)
        .order_by("-collected_at")
        .values_list("collected_at", flat=True)
        .first()
    )
    if newest is None:
        return []
    return list(
        M.Lab._default_manager.filter(user=user, panel=panel, collected_at=newest).order_by(
            "analyte"
        )
    )


@login_required
def labs_list(request):
    panels = (
        M.Lab._default_manager.filter(user=request.user)
        .values("panel", "collected_at")
        .annotate(count=Count("id"), first=Min("id"))
        .order_by("-collected_at", "panel")
    )
    page = Paginator(list(panels), 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "records/labs_list.html",
        {
            "title": "Labs",
            "domain": "labs",
            "page_obj": page,
            "pager_query": params.urlencode(),
            "panels": [
                {
                    "panel": row["panel"],
                    "collected_at": _when(row["collected_at"]),
                    "count": row["count"],
                    "url": reverse("records:labs_detail", args=[row["first"]]),
                }
                for row in page.object_list
            ],
            "state": M.domain_state(request.user, "labs"),
        },
    )


@login_required
def labs_detail(request, pk):
    row = get_object_or_404(M.Lab._default_manager.filter(user=request.user), pk=pk)
    return render(
        request,
        "records/labs_detail.html",
        {
            "title": row.panel,
            "lab_name": row.lab_name,
            "collected_at": _when(row.collected_at),
            "fasting": {None: "\u2014", True: "Yes", False: "No"}[row.fasting],
            "rows": [
                {
                    "analyte": lab.analyte,
                    "value": display(lab, "value") if lab.value is not None else lab.value_text,
                    "unit": lab.unit,
                    "reference": _reference(lab),
                    "edit_url": reverse("records:labs_edit", args=[lab.pk]),
                    "delete_url": reverse("records:labs_delete", args=[lab.pk]),
                }
                for lab in _lab_group(request.user, row).order_by("analyte")
            ],
        },
    )


LabAnalyteFormSet = formset_factory(F.LabAnalyteForm, extra=12)


@login_required
def labs_new(request):
    user = request.user
    panel_name = request.GET.get("panel", "").strip()
    if not panel_name:
        latest = M.Lab._default_manager.filter(user=user).order_by("-collected_at").first()
        panel_name = latest.panel if latest else ""
    previous = _previous_lab_panel(user, panel_name)
    initial = {"panel": panel_name}
    if previous:
        initial.update(lab_name=previous[0].lab_name, fasting=previous[0].fasting)
    panel_form = F.LabPanelForm(request.POST or None, initial=initial)
    formset = LabAnalyteFormSet(
        request.POST or None,
        prefix="analyte",
        initial=[
            {
                "analyte": lab.analyte,
                "unit": lab.unit,
                "ref_low": lab.ref_low,
                "ref_high": lab.ref_high,
            }
            for lab in previous
        ],
    )
    # A prefilled row the owner did not touch is not a row; only changed rows are validated.
    for form in formset.forms:
        form.empty_permitted = form.empty_permitted or not form.has_changed()
    error = None
    if request.method == "POST" and panel_form.is_valid() and formset.is_valid():
        rows = [form for form in formset.forms if form.has_changed()]
        if not rows:
            error = "Add at least one analyte."
        else:
            with transaction.atomic():
                first = None
                for form in rows:
                    lab = form.save(commit=False)
                    lab.user = user
                    lab.panel = panel_form.cleaned_data["panel"]
                    lab.collected_at = panel_form.cleaned_data["collected_at"]
                    lab.lab_name = panel_form.cleaned_data["lab_name"]
                    lab.fasting = panel_form.cleaned_data["fasting"]
                    lab.save()
                    first = first or lab
            audit(user, "create", "records.Lab", first.pk)
            messages.success(request, "Panel saved.")
            return see_other(reverse("records:labs_detail", args=[first.pk]))
    if error:
        messages.error(request, error)
    return render(
        request,
        "records/labs_form.html",
        {
            "title": "Add a lab panel",
            "panel_form": panel_form,
            "formset": formset,
            "known_panels": M.Lab._default_manager.filter(user=user)
            .values_list("panel", flat=True)
            .distinct()
            .order_by("panel"),
            "cancel_url": reverse("records:labs_list"),
        },
    )


class LabUpdate(RecordUpdate):
    """Editing a result's panel metadata edits the panel, so the header cannot disagree."""

    model = M.Lab
    form_class = F.LabForm
    title = "Edit result"
    success_url_name = "records:labs_detail"

    def form_valid(self, form):
        # The row and its siblings are one write: two overlapping edits of the same panel
        # must not leave rows with different lab names.
        with transaction.atomic():
            response = super().form_valid(form)
            M.Lab._default_manager.filter(
                user=self.request.user,
                panel=self.object.panel,
                collected_at=self.object.collected_at,
            ).exclude(pk=self.object.pk).update(
                lab_name=self.object.lab_name,
                fasting=self.object.fasting,
                updated_at=timezone.now(),
            )
        return response


LabEdit = LabUpdate.as_view()

LabDelete = type(
    "labs_delete",
    (RecordDelete,),
    {
        "model": M.Lab,
        "title": "Delete result",
        "success_url_name": "records:labs_list",
    },
).as_view()


# --- Dashboard and context singletons --------------------------------------------------


@login_required
def dashboard(request):
    today = timezone.localdate()
    user = request.user
    review = [
        (obj, "injuries")
        for obj in M.Injury._default_manager.filter(
            user=user, source="client", verification_status="unconfirmed"
        )
    ] + [
        (obj, "conditions")
        for obj in M.Condition._default_manager.filter(
            user=user, source="client", verification_status="unconfirmed"
        )
    ]
    slots = S.planned_slots(user, today, today)
    lines = summary.domain_lines(user)
    profile = {"identity", "environment", "conditions", "allergies", "medications"}
    context = {
        "title": "Today",
        "lines": lines,
        "profile_lines": [line for line in lines if line["domain"] in profile],
        "other_lines": [line for line in lines if line["domain"] not in profile],
        "review_rows": [
            {"text": str(obj), "url": reverse(f"records:{kind}_detail", args=[obj.pk])}
            for obj, kind in review
        ],
        "appointments": M.Appointment._default_manager.filter(
            user=user, at__gte=timezone.now(), status="planned"
        ).order_by("at")[:3],
        "slots": slots,
        "dose_groups": summary.dose_groups(slots),
        "all_taken": bool(slots) and all(slot["status"] == "taken" for slot in slots),
        "vitals": summary.vitals(user),
        "recent": summary.recent_activity(user),
        "measurement_form": F.BloodPressureForm(user=user),
        "today": today,
        "greeting": _greeting(),
        "review_url": reverse("records:memory_review"),
        "follow_ups": M.MemoryEntry._default_manager.filter(
            user=user, kind="follow_up", status="active", due_at__lte=timezone.now()
        ).order_by("due_at")[:5],
    }
    return render(request, "records/dashboard.html", context)


def _greeting():
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    return "Good afternoon" if hour < 18 else "Good evening"


def _singleton_form(request, form_class, instance, title, url_name, kind):
    form = form_class(request.POST or None, user=request.user, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request.user, "update", kind, instance.pk or "")
        messages.success(request, "Saved.")
        return see_other(reverse(url_name))
    return render(
        request,
        "records/form.html",
        {
            "form": form,
            "title": title,
            "submit_label": "Save",
            "cancel_url": reverse("records:dashboard"),
        },
    )


@login_required
def person(request):
    instance = M.Person._default_manager.filter(user=request.user).first() or M.Person(
        user=request.user
    )
    return _singleton_form(
        request, F.PersonForm, instance, "Person", "records:person", "records.Person"
    )


@login_required
def environment(request):
    instance = M.Environment._default_manager.filter(user=request.user).first() or M.Environment(
        user=request.user
    )
    return _singleton_form(
        request,
        F.EnvironmentForm,
        instance,
        "Environment",
        "records:environment",
        "records.Environment",
    )


@login_required
def program(request):
    wanted = request.GET.get("id", "")
    if wanted.isdigit():
        instance = get_object_or_404(
            M.Program._default_manager.filter(user=request.user), pk=int(wanted)
        )
    else:
        instance = M.Program._default_manager.filter(user=request.user).order_by(
            "-started", "-created_at"
        ).first() or M.Program(user=request.user)
    return _singleton_form(
        request, F.ProgramForm, instance, "Programme", "records:program", "records.Program"
    )


@login_required
def set_domain_state(request, domain):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    url_name = DOMAIN_URLS.get(domain)
    if url_name is None:
        raise Http404
    state = request.POST.get("state")
    if state not in ("unknown", "none"):
        raise Http404
    M.DomainState._default_manager.update_or_create(
        user=request.user, domain=domain, defaults={"state": state}
    )
    audit(request.user, "domain_state", "records.DomainState", detail={"state": state})
    return see_other(reverse(url_name))


DOMAIN_URLS = {
    "goals": "records:goals_list",
    "conditions": "records:conditions_list",
    "injuries": "records:injuries_list",
    "allergies": "records:allergies_list",
    "medications": "records:medications_list",
    "vaccinations": "records:vaccinations_list",
    "appointments": "records:appointments_list",
    "measurements": "records:measurements_list",
    "symptoms": "records:symptoms_list",
    "checkins": "records:checkins_list",
    "notes": "records:notes_list",
    "labs": "records:labs_list",
    "memory": "records:memory_list",
}


# --- Quick add -------------------------------------------------------------------------


def quick_form(request, form_class, title, kind):
    form = form_class(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request.user, "create", kind)
        notify_warnings(request, form)
        messages.success(request, "Saved.")
        return see_other(reverse("records:dashboard"))
    return render(
        request,
        "records/form.html",
        {
            "form": form,
            "title": title,
            "submit_label": "Save",
            "cancel_url": reverse("records:quick"),
            "compact": True,
        },
    )


@login_required
def quick(request):
    return render(request, "records/quick.html", {"title": "Quick add"})


@login_required
def quick_measurement(request):
    form = F.BloodPressureForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request.user, "create", "records.Measurement")
        notify_warnings(request, form)
        messages.success(request, "Reading saved.")
        return see_other(reverse("records:dashboard"))
    return render(
        request,
        "records/measurement.html",
        {"form": form, "title": "Blood pressure"},
    )


@login_required
def quick_symptom(request):
    return quick_form(request, F.SymptomForm, "Symptom", "records.Symptom")


@login_required
def quick_note(request):
    return quick_form(request, F.NoteForm, "Note", "records.Note")


@login_required
def quick_checkin(request):
    return quick_form(request, F.CheckinForm, "Check-in", "records.Checkin")


@login_required
def quick_period(request):
    return quick_form(request, F.PeriodForm, "Period", "records.Period")


@login_required
def quick_dose(request):
    if request.method == "POST":
        medication_id = request.POST.get("medication_id", "")
        if not medication_id.isdigit():
            raise Http404
        medication = get_object_or_404(
            M.Medication._default_manager.filter(user=request.user), pk=int(medication_id)
        )
        status = request.POST.get("status")
        if status not in R.DOSE_STATUS:
            raise Http404
        raw_slot = request.POST.get("scheduled_for") or ""
        scheduled_for = parse_datetime(raw_slot)
        if raw_slot and scheduled_for is None:
            return HttpResponse("Invalid scheduled_for.", status=400, content_type="text/plain")
        if scheduled_for and timezone.is_naive(scheduled_for):
            scheduled_for = timezone.make_aware(scheduled_for)
        if scheduled_for is None:
            M.DoseEvent._default_manager.create(
                user=request.user, medication=medication, status=status, at=timezone.now()
            )
        else:
            M.DoseEvent._default_manager.get_or_create(
                user=request.user,
                medication=medication,
                scheduled_for=scheduled_for,
                defaults={"status": status, "at": timezone.now()},
            )
        audit(request.user, "dose", "records.DoseEvent")
        messages.success(request, "Dose logged.")
        return see_other(reverse("records:dashboard"))
    today = timezone.localdate()
    return render(
        request,
        "records/quick_dose.html",
        {
            "title": "Log a dose",
            "slots": S.planned_slots(request.user, today, today),
            "today": today,
            "prn_courses": M.Medication._default_manager.filter(
                user=request.user, schedule__type="prn", stopped__isnull=True
            ).order_by("substance"),
        },
    )


@login_required
def regimen(request):
    courses = M.Medication._default_manager.filter(user=request.user).order_by(
        "stopped", "-started"
    )
    rows = [
        {
            "course": course,
            "schedule": S.describe_schedule(course.schedule),
            "doses": course.dose_events.count(),
            "detail_url": reverse("records:medications_detail", args=[course.pk]),
        }
        for course in courses
    ]
    return render(request, "records/regimen.html", {"title": "Regimen", "rows": rows})


@login_required
def timeline(request):
    return render(
        request,
        "records/timeline.html",
        {"title": "Timeline", "items": summary.recent_activity(request.user, limit=100)},
    )


# --- Memory, tokens, settings and audit -----------------------------------------------


def _paged(request, queryset, per_page=50, **context):
    page = Paginator(queryset, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context.update(entries=page.object_list, page_obj=page, pager_query=params.urlencode())
    return context


@login_required
def memory_list(request):
    query = M.MemoryEntry._default_manager.filter(user=request.user)
    status = request.GET.get("status", "active")
    if status not in R.MEMORY_STATUS + ("all",):
        status = "active"
    if status in R.MEMORY_STATUS:
        query = query.filter(status=status)
    kind = request.GET.get("kind", "")
    if kind in R.MEMORY_KIND:
        query = query.filter(kind=kind)
    subject = R.normalise_subject(request.GET.get("subject", ""))
    if subject:
        query = query.filter(subject__iexact=subject)
    search = request.GET.get("q", "").strip()
    if search:
        query = query.filter(Q(statement__icontains=search) | Q(subject__icontains=search))
    context = _paged(
        request,
        query,
        title="Memory",
        statuses=R.MEMORY_STATUS,
        kinds=R.MEMORY_KIND,
        filters={"status": status, "kind": kind, "subject": subject, "q": search},
        subjects=M.MemoryEntry._default_manager.filter(user=request.user)
        .values_list("subject", flat=True)
        .distinct()
        .order_by("subject"),
    )
    return render(request, "records/memory_list.html", context)


def _memory_entry(request, pk):
    return get_object_or_404(M.MemoryEntry._default_manager.filter(user=request.user), pk=pk)


@login_required
def memory_detail(request, pk):
    entry = _memory_entry(request, pk)
    ancestors = []
    node = entry
    while node.corrects_id:
        node = M.MemoryEntry._default_manager.get(pk=node.corrects_id, user=request.user)
        ancestors.append(node)
    return render(
        request,
        "records/memory_detail.html",
        {
            "title": "Memory entry",
            "entry": entry,
            "ancestors": ancestors,
            "corrections": entry.corrected_by.all(),
            "tags": ", ".join(entry.tags),
        },
    )


@login_required
def memory_new(request):
    form = F.MemoryForm(
        request.POST or None,
        user=request.user,
        initial={"observed_at": timezone.localtime().replace(second=0, microsecond=0)},
    )
    if request.method == "POST" and form.is_valid():
        entry = form.save()
        audit(request.user, "create", "records.MemoryEntry", entry.pk)
        messages.success(request, "Saved.")
        return see_other(reverse("records:memory_detail", args=[entry.pk]))
    return render(
        request,
        "records/form.html",
        {
            "form": form,
            "title": "Add a memory entry",
            "submit_label": "Save",
            "cancel_url": reverse("records:memory_list"),
        },
    )


@login_required
def memory_edit(request, pk):
    entry = _memory_entry(request, pk)
    if entry.locked:
        messages.error(request, "This entry is locked; unlock it before editing it.")
        return see_other(reverse("records:memory_detail", args=[entry.pk]))
    form = F.MemoryEditForm(request.POST or None, user=request.user, instance=entry)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request.user, "update", "records.MemoryEntry", entry.pk)
        messages.success(request, "Saved.")
        return see_other(reverse("records:memory_detail", args=[entry.pk]))
    return render(
        request,
        "records/form.html",
        {
            "form": form,
            "title": "Edit wording or tags",
            "submit_label": "Save",
            "cancel_url": reverse("records:memory_detail", args=[entry.pk]),
        },
    )


@login_required
def memory_correct(request, pk):
    old = _memory_entry(request, pk)
    if old.locked:
        messages.error(request, "This entry is locked; unlock it before correcting it.")
        return see_other(reverse("records:memory_detail", args=[old.pk]))
    if old.status != "active":
        messages.error(request, "This claim was already corrected; correct its replacement.")
        latest = old.corrected_by.order_by("-observed_at").first()
        return see_other(reverse("records:memory_detail", args=[(latest or old).pk]))
    instance = M.MemoryEntry(user=request.user, corrects=old, subject=old.subject)
    form = F.MemoryCorrectionForm(request.POST or None, user=request.user, instance=instance)
    if request.method == "GET":
        form.initial.update(
            subject=old.subject,
            observed_at=timezone.localtime().replace(second=0, microsecond=0),
            importance=old.importance,
            tags=", ".join(old.tags),
        )
    if request.method == "POST" and form.is_valid():
        try:
            entry = form.save()
        except ValidationError:
            messages.error(request, "This claim changed; open it again before correcting it.")
            return see_other(reverse("records:memory_detail", args=[old.pk]))
        audit(request.user, "correct", "records.MemoryEntry", entry.pk)
        messages.success(request, "Saved as a correction; the old claim is superseded.")
        return see_other(reverse("records:memory_detail", args=[entry.pk]))
    return render(
        request,
        "records/form.html",
        {
            "form": form,
            "title": "Correct the claim",
            "submit_label": "Save correction",
            "cancel_url": reverse("records:memory_detail", args=[old.pk]),
        },
    )


@login_required
def memory_delete(request, pk):
    entry = _memory_entry(request, pk)
    if entry.locked:
        messages.error(request, "This entry is locked; unlock it before deleting it.")
        return see_other(reverse("records:memory_detail", args=[entry.pk]))
    if request.method != "POST":
        return render(request, "records/delete.html", {"title": "Forget this entry"})
    object_id = entry.pk
    try:
        entry.delete()
    except ProtectedError:
        messages.error(request, "A newer entry corrects this one; delete that first.")
        return see_other(reverse("records:memory_detail", args=[entry.pk]))
    audit(request.user, "delete", "records.MemoryEntry", object_id)
    messages.success(request, "Deleted.")
    return see_other(reverse("records:memory_list"))


def _toggle(request, pk, field, label):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    entry = _memory_entry(request, pk)
    setattr(entry, field, not getattr(entry, field))
    entry.save(update_fields=[field, "updated_at"])
    audit(request.user, "update", "records.MemoryEntry", entry.pk, {field: getattr(entry, field)})
    messages.success(request, f"{label} off." if not getattr(entry, field) else f"{label} on.")
    return see_other(reverse("records:memory_detail", args=[entry.pk]))


memory_pin = login_required(partial(_toggle, field="pinned", label="Pinned"))
memory_lock = login_required(partial(_toggle, field="locked", label="Locked"))


@login_required
def memory_done(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    entry = _memory_entry(request, pk)
    entry.status = "done"
    entry.save(update_fields=["status", "updated_at"])
    audit(request.user, "update", "records.MemoryEntry", entry.pk, {"status": "done"})
    messages.success(request, "Follow-up closed.")
    return see_other(reverse("records:memory_detail", args=[entry.pk]))


REVIEW_KINDS = {
    "injuries": (M.Injury, "records:injuries_detail", "records:injuries_edit"),
    "conditions": (M.Condition, "records:conditions_detail", "records:conditions_edit"),
}

SPOT_MODELS = (
    (M.MemoryEntry, "records:memory_detail", {}),
    (M.Note, "records:notes_detail", {}),
    (M.Measurement, "records:measurements_detail", {"parent__isnull": True}),
    (M.Symptom, "records:symptoms_detail", {}),
    (M.Checkin, "records:checkins_detail", {}),
    (M.Period, "records:periods_detail", {}),
    (M.Appointment, "records:appointments_detail", {}),
)


@login_required
def memory_review(request):
    user = request.user
    if request.method == "POST":
        model, _, _ = REVIEW_KINDS.get(request.POST.get("kind", ""), (None, None, None))
        action = request.POST.get("action")
        if model is None or action not in ("confirm", "dismiss"):
            raise Http404
        if not request.POST.get("id", "").isdigit():
            raise Http404
        obj = get_object_or_404(
            model._default_manager.filter(user=user, source="client"), pk=int(request.POST["id"])
        )
        obj.verification_status = "confirmed" if action == "confirm" else "refuted"
        obj.save(update_fields=["verification_status", "updated_at"])
        audit(user, f"review_{action}", model._meta.label, obj.pk)
        messages.success(request, "Confirmed." if action == "confirm" else "Dismissed.")
        return see_other(reverse("records:memory_review"))
    rows = [
        {
            "text": str(obj),
            "kind": slug,
            "id": obj.pk,
            "url": reverse(url_name, args=[obj.pk]),
            "edit_url": reverse(edit_url, args=[obj.pk]),
            "reported_by": obj.reported_by or obj.source,
        }
        for slug, (model, url_name, edit_url) in REVIEW_KINDS.items()
        for obj in model._default_manager.filter(
            user=user, source="client", verification_status="unconfirmed"
        )
    ]
    spot = sorted(
        (
            (obj.created_at, str(obj), model._meta.verbose_name, url_name, obj.pk)
            for model, url_name, extra in SPOT_MODELS
            for obj in model._default_manager.filter(user=user, source="client", **extra).order_by(
                "-created_at"
            )[:5]
        ),
        reverse=True,
    )[:20]
    return render(
        request,
        "records/memory_review.html",
        {
            "title": "Review client writes",
            "rows": rows,
            "spot": [
                {"text": text, "kind": kind, "url": reverse(url_name, args=[pk])}
                for _, text, kind, url_name, pk in spot
            ],
        },
    )


@login_required
def promote_symptom(request, pk, target):
    symptom = get_object_or_404(M.Symptom._default_manager.filter(user=request.user), pk=pk)
    onset = timezone.localtime(symptom.onset_at).date()
    if target == "injury":
        form_class, label, url_name = F.InjuryForm, "records.Injury", "records:injuries_detail"
        initial = {"since": onset, "area": symptom.body_area or "other", "side": symptom.side}
    elif target == "condition":
        form_class, label, url_name = (
            F.ConditionForm,
            "records.Condition",
            "records:conditions_detail",
        )
        initial = {"name": symptom.name, "since": onset, "note": symptom.note}
    else:
        raise Http404
    form = form_class(request.POST or None, user=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        audit(request.user, "promote", label, obj.pk)
        messages.success(request, "Created from the symptom; the symptom is unchanged.")
        return see_other(reverse(url_name, args=[obj.pk]))
    return render(
        request,
        "records/form.html",
        {
            "form": form,
            "title": f"Promote symptom to {target}",
            "submit_label": "Save",
            "cancel_url": reverse("records:symptoms_detail", args=[symptom.pk]),
        },
    )


@login_required
def tokens(request):
    user = request.user
    new_token = None
    form = F.TokenForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        token, new_token = M.ApiToken.issue(
            user, form.cleaned_data["name"], form.cleaned_data.get("expires_at")
        )
        audit(user, "token_issue", "records.ApiToken", token.pk)
        # Rendered once in this response: never a redirect, a message or the session, so
        # the secret is never stored anywhere.
        form = F.TokenForm(user=user)
        messages.success(request, "Token issued. Copy it now.")
    return render(
        request,
        "records/tokens.html",
        {
            "title": "Tokens",
            "form": form,
            "rows": M.ApiToken._default_manager.filter(user=user),
            "new_token": new_token,
        },
    )


@login_required
def token_revoke(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    token = get_object_or_404(M.ApiToken._default_manager.filter(user=request.user), pk=pk)
    token.revoke()
    audit(request.user, "token_revoke", "records.ApiToken", token.pk)
    messages.success(request, "Token revoked.")
    return see_other(reverse("records:tokens"))


@login_required
def settings_page(request):
    user = request.user
    person = M.Person._default_manager.filter(user=user).first()
    return render(
        request,
        "records/settings.html",
        {
            "title": "Settings",
            "person": person,
            "tokens": M.ApiToken._default_manager.filter(
                user=user, revoked_at__isnull=True
            ).count(),
            "version": settings.APP_VERSION,
        },
    )


@login_required
def audit_log(request):
    context = _paged(
        request,
        M.AuditEvent._default_manager.filter(user=request.user),
        per_page=100,
        title="Audit",
    )
    return render(request, "records/audit.html", context)


# --- Export, purge, search and llms.txt -----------------------------------------------

# Tokens hold credential material and the audit trail holds no records, so neither is
# exported or purged with the health data.
SKIPPED_MODELS = (M.ApiToken, M.AuditEvent)


def data_models():
    """Every owner-scoped record model, in app order."""
    return [
        model
        for model in apps.get_app_config("records").get_models()
        if model not in SKIPPED_MODELS
    ]


@login_required
def export_data(request):
    user = request.user
    # One snapshot: a concurrent write must not leave the zip with a dose whose course is
    # missing. SQLite's IMMEDIATE transaction mode takes the writer lock for the whole read.
    with transaction.atomic():
        payload = {
            "exported_at": timezone.now().isoformat(),
            "app_version": settings.APP_VERSION,
            "registry_version": R.REGISTRY_VERSION,
            "tables": {
                model._meta.label: [
                    {
                        field.name: field.value_from_object(obj)
                        for field in model._meta.concrete_fields
                    }
                    for obj in model._default_manager.filter(user=user)
                ]
                for model in data_models()
            },
        }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "health-info-export.json",
            json.dumps(payload, cls=DjangoJSONEncoder, indent=1),
        )
    audit(user, "export", "records", detail={"tables": len(payload["tables"])})
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="health-info-export.zip"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
def purge(request):
    if request.method != "POST":
        return render(request, "records/purge.html", {"title": "Delete all health data"})
    if request.POST.get("confirm") != "purge":
        messages.error(request, "Type purge to confirm.")
        return see_other(reverse("records:purge"))
    user = request.user
    counts = {}
    with transaction.atomic():
        # PROTECT: dose events block their course, corrections block the claim they correct.
        memory = M.MemoryEntry._default_manager.filter(user=user)
        memory.update(corrects=None)
        order = [M.MemoryEntry, M.DoseEvent] + [
            model for model in data_models() if model not in (M.MemoryEntry, M.DoseEvent)
        ]
        for model in order:
            deleted, _ = model._default_manager.filter(user=user).delete()
            if deleted:
                counts[model._meta.label] = deleted
    audit(user, "purge", "records", detail={"counts": counts})
    messages.success(request, "All health records deleted.")
    return see_other(reverse("records:dashboard"))


SEARCH_LIMIT = 10

# (model, searched fields, page, how the page addresses this row): None takes the row's pk,
# "" is a page with no identity of its own, "?id={pk}" is a page that shows the named row.
# Free text only: no codes and no JSON fields.
SEARCH_TARGETS = (
    (M.MemoryEntry, ("statement", "subject"), "records:memory_detail", None),
    (M.Note, ("body",), "records:notes_detail", None),
    (M.Symptom, ("name", "trigger", "note"), "records:symptoms_detail", None),
    (M.Condition, ("name", "code", "note", "abatement"), "records:conditions_detail", None),
    (M.Injury, ("mechanism", "rehab"), "records:injuries_detail", None),
    (M.Allergy, ("substance", "reaction", "note"), "records:allergies_detail", None),
    (
        M.Medication,
        ("substance", "indication", "purpose_note"),
        "records:medications_detail",
        None,
    ),
    (M.Vaccination, ("vaccine", "lot", "site"), "records:vaccinations_detail", None),
    (M.Lab, ("panel", "analyte", "lab_name"), "records:labs_detail", None),
    (M.Measurement, ("note", "device"), "records:measurements_detail", None),
    (
        M.Appointment,
        ("practitioner_name", "place", "reason", "outcome_note"),
        "records:appointments_detail",
        None,
    ),
    (M.Period, ("note",), "records:periods_detail", None),
    (M.Checkin, ("note",), "records:checkins_detail", None),
    (
        M.Practitioner,
        ("name", "specialty", "organisation", "contact"),
        "records:practitioners_detail",
        None,
    ),
    (M.Goal, ("note",), "records:goals_detail", None),
    (M.Program, ("name", "phase", "progression"), "records:program", "?id={pk}"),
    (M.Environment, ("notes",), "records:environment", ""),
)


def _search_hits(user, model, fields, query, offset):
    condition = Q()
    for name in fields:
        condition |= Q(**{f"{name}__icontains": query})
    return model._default_manager.filter(user=user).filter(condition)[
        offset : offset + SEARCH_LIMIT + 1
    ]


@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    raw_offset = request.GET.get("offset", "0")
    offset = int(raw_offset) if raw_offset.isdigit() else 0
    results = []
    more = []
    search_url = reverse("records:search")
    if query:
        for model, fields, url_name, address in SEARCH_TARGETS:
            if kind and kind != url_name:
                continue
            hits = list(_search_hits(request.user, model, fields, query, offset))
            for obj in hits[:SEARCH_LIMIT]:
                results.append(
                    {
                        "text": str(obj),
                        "kind": model._meta.verbose_name,
                        "url": reverse(url_name, args=[obj.pk])
                        if address is None
                        else reverse(url_name) + address.format(pk=obj.pk),
                    }
                )
            if len(hits) > SEARCH_LIMIT:
                more.append(
                    {
                        "label": model._meta.verbose_name_plural,
                        "url": search_url
                        + "?"
                        + urlencode(
                            {"q": query, "kind": url_name, "offset": offset + SEARCH_LIMIT}
                        ),
                    }
                )
    back = (
        search_url
        + "?"
        + urlencode({"q": query, "kind": kind, "offset": max(0, offset - SEARCH_LIMIT)})
        if kind and offset
        else ""
    )
    return render(
        request,
        "records/search.html",
        {
            "title": "Search",
            "query": query,
            "results": results,
            "more": more,
            "kind": kind,
            "offset": offset,
            "back": back,
            "all_url": search_url + "?" + urlencode({"q": query}) if kind else "",
        },
    )


@login_required
def llms_txt(request):
    return render(
        request,
        "records/llms.txt",
        {
            "base_url": request.build_absolute_uri("/api/v1"),
            "app_version": settings.APP_VERSION,
            "registry_version": R.REGISTRY_VERSION,
        },
        content_type="text/plain; charset=utf-8",
    )


# --- Session auth ---------------------------------------------------------------------


class LoginView(auth_views.LoginView):
    template_name = "records/login.html"

    def post(self, request, *args, **kwargs):
        return lockout.guard_login(
            request, lambda: super(LoginView, self).post(request, *args, **kwargs)
        )
