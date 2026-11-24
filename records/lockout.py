"""One login lockout, shared by the owner's sign-in page and Django admin."""

from datetime import timedelta

from django.http import HttpResponse
from django.utils import timezone

LOCKOUT_LIMIT = 5
LOCKOUT_WINDOW = timedelta(minutes=15)
# In-process by design: a restart clears it, and the proxy zone is the second limit.
FAILURES = {}


def login_key(request):
    return (
        request.POST.get("username", "").strip().lower(),
        request.META.get("REMOTE_ADDR", ""),
    )


def recent(key):
    now = timezone.now()
    attempts = [moment for moment in FAILURES.get(key, []) if now - moment < LOCKOUT_WINDOW]
    if attempts:
        FAILURES[key] = attempts
    return attempts


def is_locked(key):
    return len(recent(key)) >= LOCKOUT_LIMIT


def record_failure(key):
    FAILURES[key] = [*recent(key), timezone.now()]


def clear(key):
    FAILURES.pop(key, None)


def too_many():
    return HttpResponse("Too many sign-in attempts.", status=429, content_type="text/plain")


def guard_login(request, authenticate):
    """Wrap a login view: refuse while locked, count failures, clear on success."""
    key = login_key(request)
    if request.method == "POST" and is_locked(key):
        return too_many()
    response = authenticate()
    if request.method == "POST":
        if request.user.is_authenticated:
            clear(key)
        else:
            record_failure(key)
    return response
