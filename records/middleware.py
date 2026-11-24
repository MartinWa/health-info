"""Render and interpret dates in the account's own timezone."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from records.models import Person


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
