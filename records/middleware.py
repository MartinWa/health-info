"""Timezone activation and the API-only body limit, cache headers and JSON errors."""

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse
from django.utils import timezone

from records.models import Person

API_BODY_LIMIT = 64 * 1024


class AccountTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            zone = Person.objects.filter(user=user).values_list("timezone", flat=True).first()
            try:
                timezone.activate(ZoneInfo(zone) if zone else timezone.get_default_timezone())
            except ZoneInfoNotFoundError, ValueError:
                timezone.activate(timezone.get_default_timezone())
        return self.get_response(request)


def _error(status, code, message):
    from records import registries as R

    body = {
        "error": {"code": code, "message": message, "details": []},
        "contract_version": "1.0",
        "generated_at": timezone.now().isoformat(),
        "registry_version": R.REGISTRY_VERSION,
    }
    return HttpResponse(json.dumps(body), status=status, content_type="application/json")


class ApiGuardMiddleware:
    """A 64 KiB body limit on /api/v1, no-store responses, and JSON 404/405 bodies.

    Django's own DATA_UPLOAD_MAX_MEMORY_SIZE is the 20 MiB UI limit, so the router
    enforces its own smaller one before the body is read.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith("/api/"):
            return self.get_response(request)
        length = request.META.get("CONTENT_LENGTH") or ""
        if length.isdigit() and int(length) > API_BODY_LIMIT:
            return _error(413, "payload_too_large", "Request body must be 64 KiB or less.")
        try:
            response = self.get_response(request)
        except RequestDataTooBig:
            return _error(413, "payload_too_large", "Request body must be 64 KiB or less.")
        if response.status_code in (404, 405) and not response.get("Content-Type", "").startswith(
            "application/json"
        ):
            allowed = response.get("Allow")
            response = _error(
                response.status_code,
                "not_found" if response.status_code == 404 else "method_not_allowed",
                "Not found." if response.status_code == 404 else "Method not allowed.",
            )
            if allowed:
                response["Allow"] = allowed
        response["Cache-Control"] = "no-store"
        response["Vary"] = "Authorization"
        return response
