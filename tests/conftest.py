from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from records import models as M
from records import views as V

ROW = {
    "goals": {"metric": "body_weight", "direction": "decrease", "target": 90},
    "conditions": {"name": "sentinel-condition"},
    "injuries": {"area": "knee", "side": "left", "mechanism": "sentinel-injury"},
    "allergies": {"substance": "sentinel-allergy"},
    "medications": {"substance": "sentinel-medication", "schedule": {"type": "daily"}},
    "vaccinations": {"vaccine": "sentinel-vaccine"},
    "practitioners": {"name": "sentinel-practitioner"},
    "appointments": {"at": None, "practitioner_name": "sentinel-appointment"},
    "periods": {"kind": "travel", "start": date(2026, 11, 2)},
    "measurements": {"metric": "body_weight", "value": 90, "at": None},
    "symptoms": {"name": "sentinel-symptom", "onset_at": None},
    "checkins": {"at": None, "energy": 3},
    "notes": {"body": "sentinel-note", "at": None},
}


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("alice", password="alice-password-1")


@pytest.fixture
def stranger(db):
    return get_user_model().objects.create_user("bob", password="bob-password-1")


@pytest.fixture
def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def other_client(stranger):
    client = Client()
    client.force_login(stranger)
    return client


@pytest.fixture
def course(user):
    def make(**overrides):
        values = {
            "user": user,
            "substance": "Candesartan",
            "schedule": {"type": "daily", "times": ["08:00"]},
            "started": "2026-11-02",
        }
        values.update(overrides)
        from datetime import date

        if isinstance(values["started"], str):
            values["started"] = date.fromisoformat(values["started"])
        if isinstance(values.get("stopped"), str):
            values["stopped"] = date.fromisoformat(values["stopped"])
        return M.Medication(**values)

    return make


@pytest.fixture
def make_row(user):
    def make(slug, **overrides):
        values = dict(ROW[slug])
        for field in ("at", "onset_at"):
            if field in values and values[field] is None:
                values[field] = timezone.now()
        values.update(overrides)
        return V.RESOURCES[slug]["model"]._default_manager.create(user=user, **values)

    return make


@pytest.fixture
def api_token(user):
    """(row, one-time secret) for the account's client token."""
    return M.ApiToken.issue(user, "health-mcp")


@pytest.fixture
def api(api_token):
    _, raw = api_token
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


@pytest.fixture
def other_api(stranger):
    _, raw = M.ApiToken.issue(stranger, "bob-token")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
