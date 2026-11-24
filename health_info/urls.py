"""Site routing: session auth, admin, liveness probe, then the app."""

from django.contrib import admin
from django.contrib.auth.views import LogoutView, PasswordChangeView
from django.urls import include, path

from records.views import LoginView, healthz

urlpatterns = [
    path("", include("records.urls")),
    path("login/", LoginView.as_view(template_name="records/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path(
        "password/",
        PasswordChangeView.as_view(template_name="records/password.html", success_url="/"),
        name="password",
    ),
    path("admin/", admin.site.urls),
    # Anonymous root probe: no health data, no account data.
    path("healthz", healthz, name="healthz"),
]
