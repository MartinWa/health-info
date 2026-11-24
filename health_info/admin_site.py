"""Django admin on the same login lockout as the owner's own sign-in page."""

from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig

from records.lockout import guard_login


class HealthInfoAdminSite(AdminSite):
    def login(self, request, extra_context=None):
        return guard_login(
            request,
            lambda: super(HealthInfoAdminSite, self).login(request, extra_context),
        )


class HealthInfoAdminConfig(AdminConfig):
    default_site = "health_info.admin_site.HealthInfoAdminSite"
