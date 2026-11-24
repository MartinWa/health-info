"""The API contract: envelope, auth, paging, write rules and the errors that must not leak."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from django.test import Client, RequestFactory
from django.utils import timezone

from records import api as A
from records import models as M
from records import registries as R


def call(client, method, url, payload=None):
    if payload is None:
        return getattr(client, method)(url)
    return getattr(client, method)(url, data=json.dumps(payload), content_type="application/json")


def get(client, url):
    response = client.get(url)
    return response, response.json() if response["Content-Type"].startswith(
        "application/json"
    ) else None


def post(client, url, payload):
    return call(client, "post", url, payload)


def patch(client, url, payload):
    return call(client, "patch", url, payload)


def body(response):
    return json.loads(response.content)


def test_every_endpoint_requires_a_bearer_token():
    client = Client()
    for url in ("/person", "/environment", "/goals", "/summary", "/health", "/expected-doses"):
        response = client.get(f"/api/v1{url}")
        assert response.status_code == 401, url
        assert response["WWW-Authenticate"].startswith("Bearer")
        assert body(response)["error"]["code"] == "unauthorized"


def test_the_token_is_never_accepted_from_a_query_string(api_token):
    _, raw = api_token
    response = Client().get(f"/api/v1/goals?token={raw}")
    assert response.status_code == 401
    assert raw not in response.content.decode()


def test_bad_expired_and_revoked_tokens_read_the_same(user):
    expired_row, expired = M.ApiToken.issue(
        user, "expired", expires_at=timezone.now() - timedelta(minutes=1)
    )
    revoked_row, revoked = M.ApiToken.issue(user, "revoked")
    revoked_row.revoke()
    bodies = []
    for token in ("not-a-token", expired, revoked):
        response = Client().get("/api/v1/goals", HTTP_AUTHORIZATION=f"Bearer {token}")
        assert response.status_code == 401
        payload = body(response)
        payload.pop("generated_at")
        bodies.append(payload)
    assert bodies[0] == bodies[1] == bodies[2]
    assert expired_row.pk and revoked_row.pk


def test_healthz_stays_anonymous(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_every_success_carries_the_envelope(api):
    response, payload = get(api, "/api/v1/person")
    assert response.status_code == 200
    assert payload["contract_version"] == A.CONTRACT_VERSION
    assert payload["registry_version"] == R.REGISTRY_VERSION
    assert payload["generated_at"]
    assert "person" in payload


def test_api_responses_are_never_cached(api):
    response, _ = get(api, "/api/v1/goals")
    assert response["Cache-Control"] == "no-store"
    assert "Authorization" in response["Vary"]
    assert response.get("ETag") is None


def test_decimals_are_json_numbers(api):
    post(api, "/api/v1/goals", {"metric": "weight", "direction": "decrease", "target": 90.5})
    response, payload = get(api, "/api/v1/goals")
    assert isinstance(payload["items"][0]["target"], float)
    assert '"target": 90.5' in response.content.decode()


def test_collections_page_with_a_count_and_has_more(api):
    for index in range(3):
        post(api, "/api/v1/notes", {"body": f"note {index}"})
    response, payload = get(api, "/api/v1/notes?limit=2")
    assert payload["count"] == 3
    assert payload["limit"] == 2
    assert payload["offset"] == 0
    assert payload["has_more"] is True
    assert len(payload["items"]) == 2
    _, second = get(api, "/api/v1/notes?limit=2&offset=2")
    assert second["has_more"] is False
    assert len(second["items"]) == 1


def test_a_write_returns_the_resource_and_a_location(api):
    response = post(api, "/api/v1/notes", {"body": "a note"})
    assert response.status_code == 201
    assert response["Location"] == f"/api/v1/notes/{body(response)['id']}"
    assert body(response)["source"] == "client"
    assert body(response)["reported_by"] == "health-mcp"
    assert body(response)["body"] == "a note"


def test_patch_distinguishes_an_absent_field_from_null(api):
    created = post(
        api,
        "/api/v1/symptoms",
        {
            "name": "headache",
            "onset_at": "2026-11-20T09:00:00Z",
            "resolved_at": "2026-11-20T12:00:00Z",
        },
    ).json()
    untouched = patch(api, f"/api/v1/symptoms/{created['id']}", {}).json()
    assert untouched["resolved_at"].startswith("2026-11-20T12:00:00")
    cleared = patch(api, f"/api/v1/symptoms/{created['id']}", {"resolved_at": None}).json()
    assert cleared["resolved_at"] is None


def test_unknown_fields_are_422_and_the_value_is_never_echoed(api):
    sentinel = "sentinel-9f3a-do-not-echo"
    response = post(api, "/api/v1/notes", {"body": "ok", "extra_field": sentinel})
    assert response.status_code == 422
    assert sentinel not in response.content.decode()
    error = body(response)["error"]
    assert error["code"] == "invalid_request"
    assert error["details"][0]["path"] == "extra_field"


def test_a_missing_id_is_404_never_403(api):
    response = call(api, "get", "/api/v1/goals/999")
    assert response.status_code == 404
    assert body(response)["error"]["code"] == "not_found"


def test_an_unknown_verb_is_405_and_an_unknown_route_is_404(api):
    assert call(api, "delete", "/api/v1/person").status_code == 405
    response = call(api, "get", "/api/v1/nope")
    assert response.status_code == 404
    assert body(response)["error"]["code"] == "not_found"


def test_a_body_id_resolves_owner_scoped(api, other_api, course):
    medication = course()
    medication.save()
    response = post(
        other_api,
        "/api/v1/dose-events",
        {"medication_id": medication.pk, "status": "taken", "at": "2026-11-24T08:00:00Z"},
    )
    assert response.status_code == 404
    assert body(response)["error"]["details"][0]["path"] == "medication_id"


def test_a_body_limit_is_413(api):
    response = post(api, "/api/v1/notes", {"body": "x" * (64 * 1024)})
    assert response.status_code == 413
    assert body(response)["error"]["code"] == "payload_too_large"


def test_an_internal_error_hides_the_traceback():
    request = RequestFactory().get("/api/v1/goals")
    response = A.api.on_exception(request, RuntimeError("sentinel-traceback"))
    assert response.status_code == 500
    assert "sentinel-traceback" not in response.content.decode()
    assert b"Traceback" not in response.content


def test_a_retried_dose_is_a_no_op(api, course):
    medication = course()
    medication.save()
    payload = {
        "medication_id": medication.pk,
        "status": "taken",
        "at": "2026-11-24T08:30:00+01:00",
        "scheduled_for": "2026-11-24T08:00:00+01:00",
    }
    first = post(api, "/api/v1/dose-events", payload)
    assert first.status_code == 201
    again = post(api, "/api/v1/dose-events", payload)
    assert again.status_code == 200
    assert body(again)["id"] == body(first)["id"]
    assert M.DoseEvent._default_manager.filter(medication=medication).count() == 1


def test_expected_doses_carry_a_scheduled_slot(api, course):
    course().save()
    _, payload = get(api, "/api/v1/expected-doses?start=2026-11-02&end=2026-11-08")
    assert payload["count"] == 7
    slot = payload["items"][0]
    assert slot["scheduled_for"] == "2026-11-02T08:00:00+01:00"
    assert slot["date"] == "2026-11-02"


def test_adherence_counts_events_on_the_day_they_happened(api, course):
    medication = course()
    medication.save()
    _, doses = get(api, "/api/v1/expected-doses?start=2026-11-02&end=2026-11-08")
    for index, status in ((0, "taken"), (1, "taken"), (2, "skipped")):
        slot = doses["items"][index]
        post(
            api,
            "/api/v1/dose-events",
            {
                "medication_id": medication.pk,
                "status": status,
                "at": slot["scheduled_for"],
                "scheduled_for": slot["scheduled_for"],
            },
        )
    _, payload = get(api, "/api/v1/adherence?start=2026-11-02&end=2026-11-08")
    assert payload["expected"] == 7
    assert (payload["taken"], payload["skipped"], payload["unlogged"]) == (2, 1, 4)
    assert payload["coverage"] == 0.43


def test_adherence_with_no_expected_doses_has_null_coverage(api):
    _, payload = get(api, "/api/v1/adherence?start=2026-11-02&end=2026-11-08")
    assert payload["expected"] == 0
    assert payload["coverage"] is None


def test_blood_pressure_is_one_panel_with_components(api, user):
    response = post(
        api,
        "/api/v1/measurements",
        {
            "metric": "bp",
            "at": "2026-11-24T08:00:00+01:00",
            "components": {"systolic": 120, "diastolic": 80},
            "pulse": 65,
        },
    )
    assert response.status_code == 201
    panel = body(response)
    assert panel["metric"] == "blood_pressure"
    assert [component["metric"] for component in panel["components"]] == [
        "systolic_bp",
        "diastolic_bp",
    ]
    assert M.Measurement._default_manager.filter(user=user).count() == 4  # panel, 2, pulse
    component_id = panel["components"][0]["id"]
    assert call(api, "get", f"/api/v1/measurements/{component_id}").status_code == 404
    _, listed = get(api, "/api/v1/measurements?start=2026-11-01&end=2026-11-30")
    assert {item["metric"] for item in listed["items"]} == {"pulse", "blood_pressure"}


def test_blood_pressure_needs_both_components(api):
    response = post(
        api,
        "/api/v1/measurements",
        {"metric": "blood_pressure", "components": {"systolic": 120}},
    )
    assert response.status_code == 422


def test_a_unit_mismatch_and_an_unknown_metric_are_422(api):
    response = post(
        api, "/api/v1/measurements", {"metric": "body_weight", "value": 90, "unit": "lb"}
    )
    assert response.status_code == 422
    assert body(response)["error"]["code"] == "unit_mismatch"
    response = post(api, "/api/v1/measurements", {"metric": "sentinel-metric", "value": 1})
    assert response.status_code == 422
    assert "sentinel-metric" not in response.content.decode()


def test_an_outlier_is_stored_with_a_warning(api):
    response = post(api, "/api/v1/measurements", {"metric": "body_weight", "value": 12})
    assert response.status_code == 201
    assert body(response)["warnings"]
    response = post(api, "/api/v1/measurements", {"metric": "body_weight", "value": 900})
    assert response.status_code == 422


def test_client_conditions_land_unconfirmed_and_verification_is_rejected(api):
    created = post(api, "/api/v1/conditions", {"name": "sentinel-condition"}).json()
    assert created["verification_status"] == "unconfirmed"
    assert created["source"] == "client"
    response = post(api, "/api/v1/conditions", {"name": "x", "verification_status": "confirmed"})
    assert response.status_code == 422


def test_client_appointments_are_planned_and_status_is_read_only(api):
    created = post(api, "/api/v1/appointments", {"at": "2026-12-01T10:00:00+01:00"}).json()
    assert created["status"] == "planned"
    response = patch(api, f"/api/v1/appointments/{created['id']}", {"status": "attended"})
    assert response.status_code == 422


def test_a_correction_supersedes_the_claim(api):
    old = post(
        api,
        "/api/v1/memory",
        {
            "kind": "fact",
            "subject": "Sleep",
            "statement": "I need eight hours.",
            "observed_at": "2026-11-01T08:00:00Z",
        },
    ).json()
    assert old["kind"] == "fact"
    correction = post(
        api,
        "/api/v1/memory",
        {
            "kind": "correction",
            "subject": "sleep",
            "statement": "Seven hours is enough.",
            "observed_at": "2026-11-10T08:00:00Z",
            "corrects_id": old["id"],
        },
    )
    assert correction.status_code == 201
    corrected = correction.json()
    assert corrected["corrects_id"] == old["id"]
    superseded = call(api, "get", f"/api/v1/memory/{old['id']}").json()
    assert superseded["status"] == "superseded"
    assert superseded["valid_until"].startswith("2026-11-10T08:00:00")


def test_a_locked_entry_rejects_client_writes(api, user):
    locked = M.MemoryEntry._default_manager.create(
        user=user,
        kind="fact",
        subject="sleep",
        statement="Locked claim.",
        observed_at=timezone.now(),
        locked=True,
    )
    response = patch(api, f"/api/v1/memory/{locked.pk}", {"statement": "changed"})
    assert response.status_code == 422
    assert body(response)["error"]["code"] == "locked"
    assert call(api, "delete", f"/api/v1/memory/{locked.pk}").status_code == 422


def test_deleting_a_corrected_claim_is_409(api):
    old = post(
        api,
        "/api/v1/memory",
        {"kind": "fact", "subject": "a", "statement": "one", "observed_at": "2026-11-01T08:00:00Z"},
    ).json()
    post(
        api,
        "/api/v1/memory",
        {
            "kind": "correction",
            "subject": "a",
            "statement": "two",
            "observed_at": "2026-11-02T08:00:00Z",
            "corrects_id": old["id"],
        },
    )
    response = call(api, "delete", f"/api/v1/memory/{old['id']}")
    assert response.status_code == 409
    assert body(response)["error"]["code"] == "has_corrections"


def test_memory_patch_only_touches_the_five_allowed_fields(api):
    created = post(
        api,
        "/api/v1/memory",
        {
            "kind": "fact",
            "subject": "a",
            "statement": "one",
            "observed_at": "2026-11-01T08:00:00Z",
        },
    ).json()
    response = patch(api, f"/api/v1/memory/{created['id']}", {"kind": "preference"})
    assert response.status_code == 422


def test_a_follow_up_needs_a_due_date(api):
    response = post(
        api,
        "/api/v1/memory",
        {
            "kind": "follow_up",
            "subject": "a",
            "statement": "ask again",
            "observed_at": "2026-11-01T08:00:00Z",
        },
    )
    assert response.status_code == 422
    assert body(response)["error"]["details"][0]["path"] == "due_at"


def test_summary_excludes_the_name_and_birth_date(api, user):
    M.Person._default_manager.create(
        user=user, name="sentinel-name", birth_date=date(1985, 4, 3), sex_at_birth="male"
    )
    _, payload = get(api, "/api/v1/summary")
    text = json.dumps(payload)
    assert "sentinel-name" not in text
    assert "1985-04-03" not in text
    assert payload["summary"]["person"]["sex_at_birth"] == "male"
    assert payload["summary"]["person"]["age_years"] is not None
    assert set(payload["summary"]["constraints"]) == {"allergies", "injuries", "conditions"}
    assert payload["summary"]["constraints"]["allergies"]["state"] == "unknown"
    assert "allergies" in payload["summary"]["missing"]


def test_summary_counts_client_rows_without_verification(api):
    post(api, "/api/v1/injuries", {"area": "knee", "side": "left"})
    _, payload = get(api, "/api/v1/summary")
    block = payload["summary"]["constraints"]["injuries"]
    assert block["state"] == "present"
    assert block["items"][0]["verification_status"] == "unconfirmed"
    assert "injuries" not in payload["summary"]["missing"]


def test_health_reports_versions_and_counts(api):
    _, payload = get(api, "/api/v1/health")
    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["contract_version"] == A.CONTRACT_VERSION
    assert payload["registry_version"] == R.REGISTRY_VERSION
    assert payload["today"] == timezone.localdate().isoformat()
    assert payload["counts"]["measurements"] == 0


def test_the_environment_singleton_carries_its_state(api):
    _, payload = get(api, "/api/v1/environment")
    assert payload["environment"]["state"] == "unknown"
    updated = patch(api, "/api/v1/environment", {"gym_access": "full", "equipment": ["barbell"]})
    assert updated.status_code == 200
    assert updated.json()["environment"]["gym_access"] == "full"
    assert updated.json()["environment"]["state"] == "present"
    response = patch(api, "/api/v1/environment", {"equipment": ["sentinel-gear"]})
    assert response.status_code == 422


def test_openapi_is_committed_and_current():
    path = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"
    committed = path.read_text(encoding="utf-8")
    generated = json.dumps(A.api.get_openapi_schema(), indent=2, sort_keys=True) + "\n"
    assert committed == generated, "run `uv run python tools/dump_openapi.py`"


def test_api_has_no_bare_default_manager_calls():
    source = open(A.__file__, encoding="utf-8").read()
    assert ".objects." not in source


def test_the_registry_version_is_the_envelope_value(api):
    _, payload = get(api, "/api/v1/goals")
    assert payload["registry_version"] == R.REGISTRY_VERSION == 1


def test_a_measurement_at_defaults_to_now(api):
    response = post(api, "/api/v1/measurements", {"metric": "pulse", "value": 60})
    assert response.status_code == 201
    created_at = datetime.fromisoformat(response.json()["at"].replace("Z", "+00:00"))
    assert abs((timezone.now() - created_at).total_seconds()) < 60


CREATES = {
    "/api/v1/goals": {"metric": "body_weight", "direction": "decrease", "target": 90},
    "/api/v1/programs": {"name": "sentinel-programme"},
    "/api/v1/periods": {"kind": "travel", "start": "2026-11-02"},
    "/api/v1/conditions": {"name": "sentinel-condition"},
    "/api/v1/injuries": {"area": "knee", "side": "left"},
    "/api/v1/dose-events": {"at": "2026-11-24T08:00:00Z"},
    "/api/v1/measurements": {"metric": "pulse", "value": 60},
    "/api/v1/symptoms": {"name": "sentinel-symptom", "onset_at": "2026-11-24T09:00:00Z"},
    "/api/v1/checkins": {"at": "2026-11-24T09:00:00Z", "energy": 3},
    "/api/v1/notes": {"body": "sentinel-note"},
    "/api/v1/appointments": {"at": "2026-12-01T10:00:00Z"},
    "/api/v1/memory": {
        "kind": "fact",
        "subject": "sentinel",
        "statement": "sentinel-claim",
        "observed_at": "2026-11-24T09:00:00Z",
    },
}


@pytest.mark.parametrize("collection", sorted(CREATES))
def test_each_write_route_round_trips(collection, api, user):
    payload = dict(CREATES[collection])
    if collection == "/api/v1/dose-events":
        payload["medication_id"] = M.Medication._default_manager.create(
            user=user,
            substance="sentinel-medication",
            schedule={"type": "daily"},
            started=date(2026, 11, 2),
        ).pk
    created = post(api, collection, payload)
    assert created.status_code == 201, created.content
    pk = created.json()["id"]
    assert api.get(f"{collection}/{pk}").status_code == 200
    assert patch(api, f"{collection}/{pk}", {}).status_code == 200
    assert api.delete(f"{collection}/{pk}").status_code == 204


def test_valid_decimals_survive_input(api, course):
    goal = post(
        api, "/api/v1/goals", {"metric": "body_weight", "direction": "decrease", "target": 88.1}
    )
    assert goal.status_code == 201, goal.content
    assert goal.json()["target"] == 88.1
    medication = course()
    medication.save()
    dose = post(
        api,
        "/api/v1/dose-events",
        {"medication_id": medication.pk, "at": "2026-11-24T08:00:00Z", "dose": 0.1},
    )
    assert dose.status_code == 201, dose.content
    assert dose.json()["dose"] == 0.1
    measurement = post(api, "/api/v1/measurements", {"metric": "body_weight", "value": 88.1})
    assert measurement.status_code == 201, measurement.content
    patched = patch(api, f"/api/v1/goals/{goal.json()['id']}", {"target": 88.2})
    assert patched.status_code == 200, patched.content
    assert patched.json()["target"] == 88.2


def test_a_disabled_account_loses_api_access(api, user):
    user.is_active = False
    user.save(update_fields=["is_active"])
    response = api.get("/api/v1/notes")
    assert response.status_code == 401
    assert body(response)["error"]["code"] == "unauthorized"


def test_a_choice_error_never_echoes_the_value(api):
    sentinel = "sentinel-choice-value"
    response = post(api, "/api/v1/conditions", {"name": "x", "status": sentinel})
    assert response.status_code == 422
    assert sentinel not in response.content.decode()
    detail = body(response)["error"]["details"][0]
    assert detail["path"] == "status"
    assert detail["message"] == "Unknown value."


def test_item_reads_and_writes_carry_the_envelope(api):
    created = post(api, "/api/v1/notes", {"body": "enveloped"})
    for payload in (created.json(),):
        assert payload["contract_version"] == A.CONTRACT_VERSION
        assert payload["registry_version"] == R.REGISTRY_VERSION
        assert payload["generated_at"]
    note_id = created.json()["id"]
    _, fetched = get(api, f"/api/v1/notes/{note_id}")
    assert (fetched["contract_version"], fetched["registry_version"]) == (A.CONTRACT_VERSION, 1)
    patched = patch(api, f"/api/v1/notes/{note_id}", {"body": "still enveloped"}).json()
    assert patched["contract_version"] == A.CONTRACT_VERSION
    _, health = get(api, "/api/v1/health")
    assert health["generated_at"]
    _, listed = get(api, "/api/v1/notes")
    assert "contract_version" not in listed["items"][0]


def test_a_blood_pressure_panel_can_be_annotated(api):
    panel = post(
        api,
        "/api/v1/measurements",
        {
            "metric": "blood_pressure",
            "at": "2026-11-24T08:00:00+01:00",
            "components": {"systolic": 120, "diastolic": 80},
            "context": {"posture": "sitting", "side": "left"},
        },
    ).json()
    assert panel["posture"] == "sitting"
    assert panel["components"][0]["posture"] == "sitting"
    assert panel["components"][0]["side"] == "left"
    updated = patch(api, f"/api/v1/measurements/{panel['id']}", {"note": "retaken"})
    assert updated.status_code == 200, updated.content
    assert updated.json()["note"] == "retaken"
    assert patch(api, f"/api/v1/measurements/{panel['id']}", {"value": 120}).status_code == 422
    # The panel bypasses clean(), so the allowed fields are validated explicitly.
    url = f"/api/v1/measurements/{panel['id']}"
    assert patch(api, url, {"at": None}).status_code == 422
    assert patch(api, url, {"note": None}).status_code == 422
    too_long = patch(api, url, {"note": "x" * 201})
    assert too_long.status_code == 422
    assert body(too_long)["error"]["details"][0]["path"] == "note"
    assert patch(api, url, {"note": "x" * 200}).status_code == 200


def test_a_correction_can_be_pinned(api):
    old = post(
        api,
        "/api/v1/memory",
        {"kind": "fact", "subject": "a", "statement": "one", "observed_at": "2026-11-01T08:00:00Z"},
    ).json()
    correction = post(
        api,
        "/api/v1/memory",
        {
            "kind": "correction",
            "subject": "a",
            "statement": "two",
            "observed_at": "2026-11-02T08:00:00Z",
            "corrects_id": old["id"],
        },
    ).json()
    response = patch(api, f"/api/v1/memory/{correction['id']}", {"pinned": True})
    assert response.status_code == 200, response.content
    assert response.json()["pinned"] is True


def test_dates_follow_the_account_timezone(api, user, monkeypatch):
    M.Person._default_manager.update_or_create(
        user=user, defaults={"timezone": "Pacific/Kiritimati"}
    )
    instant = datetime(2026, 11, 24, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: instant)
    _, payload = get(api, "/api/v1/health")
    # 12:30 UTC is already the next day in UTC+14 and still the same day in Stockholm.
    assert payload["today"] == "2026-11-25"


def test_an_explicit_null_for_a_non_nullable_field_is_422(api):
    note = post(api, "/api/v1/notes", {"body": "x", "tags": ["a"]}).json()
    response = patch(api, f"/api/v1/notes/{note['id']}", {"tags": None})
    assert response.status_code == 422
    assert body(response)["error"]["details"][0]["path"] == "tags"
    response = patch(api, f"/api/v1/notes/{note['id']}", {"at": None})
    assert response.status_code == 422
