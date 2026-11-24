"""UI routes from the plan's §6 table, one CRUD block per resource."""

from django.urls import path

from records import views as V

app_name = "records"

urlpatterns = [
    path("", V.dashboard, name="dashboard"),
    path("context/person", V.person, name="person"),
    path("context/environment", V.environment, name="environment"),
    path("context/program", V.program, name="program"),
    path("context/goals", V.RESOURCE_VIEWS["goals"]["list"], name="goals_list"),
    path("goals/new", V.RESOURCE_VIEWS["goals"]["new"], name="goals_new"),
    path("goals/<int:pk>", V.RESOURCE_VIEWS["goals"]["detail"], name="goals_detail"),
    path("goals/<int:pk>/edit", V.RESOURCE_VIEWS["goals"]["edit"], name="goals_edit"),
    path("goals/<int:pk>/delete", V.RESOURCE_VIEWS["goals"]["delete"], name="goals_delete"),
    path("state/<slug:domain>", V.set_domain_state, name="state"),
    path("quick", V.quick, name="quick"),
    path("quick/measurement", V.quick_measurement, name="quick_measurement"),
    path("quick/symptom", V.quick_symptom, name="quick_symptom"),
    path("quick/dose", V.quick_dose, name="quick_dose"),
    path("quick/note", V.quick_note, name="quick_note"),
    path("quick/checkin", V.quick_checkin, name="quick_checkin"),
    path("quick/period", V.quick_period, name="quick_period"),
    path("timeline", V.timeline, name="timeline"),
    path("regimen", V.regimen, name="regimen"),
]

for slug in (
    "conditions",
    "injuries",
    "allergies",
    "medications",
    "vaccinations",
    "practitioners",
    "appointments",
    "periods",
    "measurements",
    "symptoms",
    "checkins",
    "notes",
):
    views = V.RESOURCE_VIEWS[slug]
    urlpatterns += [
        path(slug, views["list"], name=f"{slug}_list"),
        path(f"{slug}/new", views["new"], name=f"{slug}_new"),
        path(f"{slug}/<int:pk>", views["detail"], name=f"{slug}_detail"),
        path(f"{slug}/<int:pk>/edit", views["edit"], name=f"{slug}_edit"),
        path(f"{slug}/<int:pk>/delete", views["delete"], name=f"{slug}_delete"),
    ]
