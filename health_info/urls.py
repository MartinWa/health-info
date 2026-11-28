"""Site routing: session auth, admin, liveness probe, the JSON API, then the app."""

from django.contrib import admin
from django.contrib.auth.views import LogoutView, PasswordChangeView
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import RedirectView

from records.api import api
from records.views import LoginView, healthz

urlpatterns = [
    path("api/v1/", api.urls),
    path("", include("records.urls")),
    path("login/", LoginView.as_view(template_name="records/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path(
        "password/",
        PasswordChangeView.as_view(template_name="records/password.html", success_url="/"),
        name="password",
    ),
    path("admin/", admin.site.urls),
    # Browsers and link unfurlers ask for this path even with a <link rel="icon">.
    path("favicon.ico", RedirectView.as_view(url=static("records/favicon.svg"), permanent=True)),
    # Anonymous root probe: no health data, no account data.
    path("healthz", healthz, name="healthz"),
]
