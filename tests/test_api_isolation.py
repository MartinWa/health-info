"""API isolation, parameterised from the committed schema so a new route cannot be missed."""

import json
from datetime import date
from pathlib import Path

import pytest
from django.test import Client
from django.utils import timezone

from records import models as M

SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json").read_text(
        encoding="utf-8"
    )
)

SENTINEL = "sentinel"


def _seed_medication(user):
    return M.Medication._default_manager.create(
        user=user,
        substance=f"{SENTINEL}-medication",
        schedule={"type": "daily", "times": ["08:00"]},
        started=timezone.localdate(),
    )


def factories():
    """One owner row per collection path; every success body carries the sentinel."""

    def medication(user):
        return _seed_medication(user)

    return {
        "/api/v1/goals": lambda user: M.Goal._default_manager.create(
            user=user, metric="body_weight", direction="decrease", target=90
        ),
        "/api/v1/programs": lambda user: M.Program._default_manager.create(
            user=user, name=f"{SENTINEL}-program"
        ),
        "/api/v1/events": lambda user: M.Event._default_manager.create(
            user=user, kind="travel", start=date(2026, 11, 2)
        ),
        "/api/v1/conditions": lambda user: M.Condition._default_manager.create(
            user=user, name=f"{SENTINEL}-condition"
        ),
        "/api/v1/injuries": lambda user: M.Injury._default_manager.create(
            user=user, area="knee", side="left", mechanism=SENTINEL
        ),
        "/api/v1/doses": lambda user: M.Dose._default_manager.create(
            user=user, medication=medication(user), at=timezone.now(), reason=SENTINEL
        ),
        "/api/v1/measurements": lambda user: M.Measurement._default_manager.create(
            user=user, metric="body_weight", value=90, unit="kg", at=timezone.now()
        ),
        "/api/v1/symptoms": lambda user: M.Symptom._default_manager.create(
            user=user, name=f"{SENTINEL}-symptom", onset_at=timezone.now()
        ),
        "/api/v1/checkins": lambda user: M.Checkin._default_manager.create(
            user=user, at=timezone.now(), note=SENTINEL
        ),
        "/api/v1/notes": lambda user: M.Note._default_manager.create(
            user=user, at=timezone.now(), body=f"{SENTINEL}-note"
        ),
        "/api/v1/appointments": lambda user: M.Appointment._default_manager.create(
            user=user, at=timezone.now(), place=SENTINEL
        ),
        "/api/v1/memory": lambda user: M.MemoryEntry._default_manager.create(
            user=user,
            kind="fact",
            subject="sleep",
            statement=f"{SENTINEL}-claim",
            observed_at=timezone.now(),
        ),
        "/api/v1/allergies": lambda user: M.Allergy._default_manager.create(
            user=user, substance=f"{SENTINEL}-allergy"
        ),
        "/api/v1/medications": medication,
        "/api/v1/labs": lambda user: M.Lab._default_manager.create(
            user=user,
            panel=SENTINEL,
            analyte="analyte",
            value=1,
            collected_at=timezone.now(),
        ),
        "/api/v1/vaccinations": lambda user: M.Vaccination._default_manager.create(
            user=user, vaccine=f"{SENTINEL}-vaccine"
        ),
        "/api/v1/practitioners": lambda user: M.Practitioner._default_manager.create(
            user=user, name=f"{SENTINEL}-practitioner"
        ),
    }


FACTORIES = factories()

ITEM_PATHS = {path: set(operations) for path, operations in SCHEMA["paths"].items() if "{" in path}

DERIVED = {"/api/v1/expected-doses", "/api/v1/adherence"}
SINGLETONS = {"/api/v1/person", "/api/v1/environment", "/api/v1/summary", "/api/v1/health"}
COLLECTIONS = set(SCHEMA["paths"]) - ITEM_PATHS.keys() - SINGLETONS


def collection_of(path):
    return path.split("/{")[0]


def test_every_item_route_has_an_isolation_case():
    # A new {id} route must map to a collection that has seeds, or this list changes.
    assert {collection_of(path) for path in ITEM_PATHS} <= set(FACTORIES)


def test_every_collection_has_a_seed():
    assert COLLECTIONS == set(FACTORIES) | DERIVED


@pytest.mark.parametrize("path", sorted(ITEM_PATHS))
def test_another_accounts_id_is_404_for_every_declared_verb(path, user, other_api):
    obj = FACTORIES[collection_of(path)](user)
    url = path.replace("{id}", str(obj.pk))
    for verb in ITEM_PATHS[path]:
        if verb == "get":
            response = other_api.get(url)
        elif verb == "patch":
            response = other_api.patch(url, data="{}", content_type="application/json")
        else:
            response = other_api.delete(url)
        assert response.status_code == 404, (path, verb)
        assert SENTINEL not in response.content.decode()


@pytest.mark.parametrize("path", sorted(COLLECTIONS))
def test_another_accounts_collection_is_empty(path, user, other_api):
    if path in DERIVED:
        _seed_medication(user)
    else:
        FACTORIES[path](user)
    response = other_api.get(path)
    assert response.status_code == 200
    payload = response.json()
    if path == "/api/v1/adherence":
        assert payload["expected"] == 0
        return
    assert payload["count"] == 0
    assert payload["items"] == []
    assert SENTINEL not in response.content.decode()


def test_owner_scoping_query_parameters_are_ignored(user, other_api):
    row = FACTORIES["/api/v1/notes"](user)
    for parameter in ("user_id", "owner", "account"):
        payload = other_api.get(f"/api/v1/notes?{parameter}={row.user_id}").json()
        assert payload["count"] == 0


def test_body_ids_are_owner_scoped(user, other_api):
    medication = FACTORIES["/api/v1/medications"](user)
    practitioner = FACTORIES["/api/v1/practitioners"](user)
    claim = FACTORIES["/api/v1/memory"](user)
    cases = [
        (
            "/api/v1/doses",
            {"medication_id": medication.pk, "at": "2026-11-24T08:00:00Z"},
            "medication_id",
        ),
        (
            "/api/v1/conditions",
            {"name": "sentinel", "practitioner_id": practitioner.pk},
            "practitioner_id",
        ),
        (
            "/api/v1/memory",
            {
                "kind": "correction",
                "subject": "sleep",
                "statement": "sentinel",
                "observed_at": "2026-11-24T08:00:00Z",
                "corrects_id": claim.pk,
            },
            "corrects_id",
        ),
    ]
    for url, payload, path in cases:
        response = other_api.post(url, data=json.dumps(payload), content_type="application/json")
        assert response.status_code == 404, url
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["details"][0]["path"] == path


def test_another_accounts_token_still_reads_nothing(user, other_api):
    FACTORIES["/api/v1/goals"](user)
    payload = other_api.get("/api/v1/summary").json()["summary"]
    assert payload["goals"] == []
    assert payload["constraints"]["conditions"]["items"] == []
    assert "sleep" not in json.dumps(payload)


def test_admin_has_no_health_model_screens(user):
    user.is_staff = True
    user.is_superuser = True
    user.save()
    admin = Client()
    admin.force_login(user)
    for slug in ("condition", "injury", "measurement", "memoryentry", "lab", "note"):
        assert admin.get(f"/admin/records/{slug}/").status_code == 404
