"""Memory rules, review, tokens and audit: the supersede chain and what never leaks."""

import hashlib
from datetime import date, datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from records import models as M

OBSERVED = "2026-11-20T09:00"


def entry(user, **overrides):
    values = {
        "user": user,
        "kind": "fact",
        "subject": "training",
        "statement": "Trains four mornings a week.",
        "observed_at": timezone.make_aware(datetime(2026, 11, 20, 9, 0)),
        "importance": 3,
    }
    values.update(overrides)
    return M.MemoryEntry._default_manager.create(**values)


def test_correction_supersedes_the_old_claim_in_one_step(user, logged_in):
    old = entry(user)
    response = logged_in.post(
        reverse("records:memory_correct", args=[old.pk]),
        {
            "subject": "training",
            "statement": "Trains three mornings a week.",
            "observed_at": OBSERVED,
            "importance": "3",
            "source_ref": "",
            "tags": "",
        },
    )
    assert response.status_code == 303
    old.refresh_from_db()
    correction = M.MemoryEntry._default_manager.get(corrects=old)
    assert correction.kind == "correction"
    assert old.status == "superseded"
    assert old.valid_until == correction.observed_at
    assert correction.status == "active"
    page = logged_in.get(reverse("records:memory_detail", args=[correction.pk]))
    assert page.status_code == 200
    assert old.statement in page.content.decode()
    assert (
        correction.statement
        in logged_in.get(reverse("records:memory_detail", args=[old.pk])).content.decode()
    )


def test_a_back_dated_correction_is_a_field_error(user, logged_in):
    original = entry(user)
    response = logged_in.post(
        reverse("records:memory_correct", args=[original.pk]),
        {
            "subject": "training",
            "statement": "Changed.",
            "observed_at": "2026-11-01T09:00",
            "importance": "3",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].errors["observed_at"]
    assert M.MemoryEntry._default_manager.filter(corrects__isnull=False).count() == 0
    original.refresh_from_db()
    assert original.status == "active"


def test_a_second_correction_of_the_same_claim_goes_to_its_replacement(user, logged_in):
    original = entry(user)
    first = M.supersede(
        original,
        M.MemoryEntry(
            user=user,
            subject="training",
            statement="Trains three mornings a week.",
            observed_at=timezone.make_aware(datetime(2026, 11, 21, 9, 0)),
        ),
    )
    response = logged_in.post(
        reverse("records:memory_correct", args=[original.pk]),
        {
            "subject": "training",
            "statement": "Competing replacement.",
            "observed_at": OBSERVED,
            "importance": "3",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"] == reverse("records:memory_detail", args=[first.pk])
    assert M.MemoryEntry._default_manager.filter(corrects=original).count() == 1
    assert M.MemoryEntry._default_manager.filter(status="active").count() == 1

    later = logged_in.post(
        reverse("records:memory_correct", args=[first.pk]),
        {
            "subject": "training",
            "statement": "Trains two mornings a week.",
            "observed_at": "2026-11-22T09:00",
            "importance": "3",
        },
    )
    assert later.status_code == 303
    assert M.MemoryEntry._default_manager.filter(status="active").count() == 1


def test_a_correction_without_a_timestamp_is_a_field_error(user, logged_in):
    original = entry(user)
    response = logged_in.post(
        reverse("records:memory_correct", args=[original.pk]),
        {"subject": "training", "statement": "Changed.", "importance": "3"},
    )
    assert response.status_code == 200
    assert response.context["form"].errors["observed_at"]
    assert M.MemoryEntry._default_manager.filter(corrects__isnull=False).count() == 0


def test_the_edit_form_does_not_ask_for_the_observation_time(user, logged_in):
    original = entry(user)
    page = logged_in.get(reverse("records:memory_edit", args=[original.pk]))
    assert page.status_code == 200
    assert "observed_at" not in page.context["form"].fields
    response = logged_in.post(
        reverse("records:memory_edit", args=[original.pk]),
        {"subject": "training", "statement": "Trains in the mornings.", "tags": ""},
    )
    assert response.status_code == 303
    original.refresh_from_db()
    assert original.statement == "Trains in the mornings."


def test_a_correction_kind_without_a_corrects_id_is_refused(user, logged_in):
    response = logged_in.post(
        reverse("records:memory_new"),
        {
            "kind": "correction",
            "subject": "training",
            "statement": "Trains three mornings a week.",
            "observed_at": OBSERVED,
            "importance": "3",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].errors["kind"]
    assert M.MemoryEntry._default_manager.filter(user=user).count() == 0


def test_locked_blocks_edit_and_delete_until_unlocked(user, logged_in):
    locked = entry(user, locked=True)
    assert logged_in.get(reverse("records:memory_edit", args=[locked.pk])).status_code == 303
    assert logged_in.post(reverse("records:memory_delete", args=[locked.pk])).status_code == 303
    assert M.MemoryEntry._default_manager.filter(pk=locked.pk).exists()


def test_editing_a_typo_leaves_claim_fields_alone(user, logged_in):
    original = entry(user, kind="preference")
    response = logged_in.post(
        reverse("records:memory_edit", args=[original.pk]),
        {
            "subject": "training",
            "statement": "Trains four mornings a week, usually.",
            "tags": "routine, gym",
            "pinned": "on",
            "kind": "correction",
            "observed_at": "2026-01-01T00:00",
        },
    )
    assert response.status_code == 303
    original.refresh_from_db()
    assert original.kind == "preference"
    assert original.observed_at == timezone.make_aware(datetime(2026, 11, 20, 9, 0))
    assert original.tags == ["routine", "gym"]
    assert original.pinned is True
    assert M.MemoryEntry._default_manager.filter(corrects__isnull=False).count() == 0


def test_a_follow_up_needs_a_due_date(user, logged_in):
    response = logged_in.post(
        reverse("records:memory_new"),
        {
            "kind": "follow_up",
            "subject": "knee",
            "statement": "Ask about the knee at the next visit.",
            "observed_at": OBSERVED,
            "importance": "3",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].errors["due_at"]
    assert M.MemoryEntry._default_manager.filter(user=user).count() == 0


def test_locked_blocks_a_correction_until_the_owner_unlocks(user, logged_in):
    locked = entry(user, locked=True)
    response = logged_in.post(
        reverse("records:memory_correct", args=[locked.pk]),
        {
            "subject": "training",
            "statement": "Changed.",
            "observed_at": OBSERVED,
            "importance": "3",
        },
    )
    assert response.status_code == 303
    assert M.MemoryEntry._default_manager.filter(corrects=locked).count() == 0
    logged_in.post(reverse("records:memory_lock", args=[locked.pk]))
    locked.refresh_from_db()
    assert locked.locked is False
    response = logged_in.post(
        reverse("records:memory_correct", args=[locked.pk]),
        {
            "subject": "training",
            "statement": "Changed.",
            "observed_at": OBSERVED,
            "importance": "3",
        },
    )
    assert response.status_code == 303
    assert M.MemoryEntry._default_manager.filter(corrects=locked).count() == 1


def test_deleting_a_corrected_claim_is_refused(user, logged_in):
    old = entry(user)
    correction = M.supersede(
        old,
        M.MemoryEntry(
            user=user,
            subject="training",
            statement="Trains three mornings a week.",
            observed_at=timezone.make_aware(datetime(2026, 11, 21, 9, 0)),
        ),
    )
    response = logged_in.post(reverse("records:memory_delete", args=[old.pk]))
    assert response.status_code == 303
    assert M.MemoryEntry._default_manager.filter(pk=old.pk).exists()
    response = logged_in.post(reverse("records:memory_delete", args=[correction.pk]))
    assert response.status_code == 303
    assert not M.MemoryEntry._default_manager.filter(pk=correction.pk).exists()


def test_closing_a_follow_up_sets_done(user, logged_in):
    follow_up = entry(
        user,
        kind="follow_up",
        statement="Ask about the knee.",
        due_at=timezone.now(),
    )
    response = logged_in.post(reverse("records:memory_done", args=[follow_up.pk]))
    assert response.status_code == 303
    follow_up.refresh_from_db()
    assert follow_up.status == "done"


def test_memory_is_owner_scoped(user, other_client):
    mine = entry(user)
    assert other_client.get(reverse("records:memory_list")).status_code == 200
    assert list(other_client.get(reverse("records:memory_list")).context["entries"]) == []
    assert other_client.get(reverse("records:memory_detail", args=[mine.pk])).status_code == 404
    for name in ("memory_edit", "memory_correct", "memory_delete"):
        url = reverse(f"records:{name}", args=[mine.pk])
        method = (
            other_client.post if name in ("memory_correct", "memory_delete") else other_client.get
        )
        assert method(url).status_code == 404


def test_memory_filters_and_search(user, logged_in):
    entry(user, kind="preference", subject="sleep", statement="Prefers a cold bedroom.")
    entry(user, kind="fact", subject="training", statement="Trains in the morning.")
    listing = logged_in.get(reverse("records:memory_list"), {"kind": "preference"})
    assert [item.subject for item in listing.context["entries"]] == ["sleep"]
    search = logged_in.get(reverse("records:memory_list"), {"q": "cold bedroom"})
    assert [item.kind for item in search.context["entries"]] == ["preference"]
    all_rows = logged_in.get(reverse("records:memory_list"), {"status": "all"})
    assert len(all_rows.context["entries"]) == 2


def test_review_confirms_and_dismisses_client_rows(user, logged_in):
    injury = M.Injury._default_manager.create(
        user=user, area="knee", side="left", source="client", verification_status="unconfirmed"
    )
    condition = M.Condition._default_manager.create(
        user=user, name="Example", source="client", verification_status="unconfirmed"
    )
    page = logged_in.get(reverse("records:memory_review"))
    assert page.status_code == 200
    assert len(page.context["rows"]) == 2

    logged_in.post(
        reverse("records:memory_review"),
        {"kind": "injuries", "id": injury.pk, "action": "confirm"},
    )
    logged_in.post(
        reverse("records:memory_review"),
        {"kind": "conditions", "id": condition.pk, "action": "dismiss"},
    )
    injury.refresh_from_db()
    condition.refresh_from_db()
    assert injury.verification_status == "confirmed"
    assert condition.verification_status == "refuted"


def test_review_never_touches_another_accounts_row(user, other_client):
    injury = M.Injury._default_manager.create(
        user=user, area="knee", source="client", verification_status="unconfirmed"
    )
    response = other_client.post(
        reverse("records:memory_review"),
        {"kind": "injuries", "id": injury.pk, "action": "confirm"},
    )
    assert response.status_code == 404
    injury.refresh_from_db()
    assert injury.verification_status == "unconfirmed"


def test_promote_a_symptom_prefills_onset_and_area(user, logged_in):
    symptom = M.Symptom._default_manager.create(
        user=user,
        name="Knee pain",
        body_area="knee",
        side="left",
        onset_at=timezone.make_aware(datetime(2026, 11, 2, 8, 0)),
        note="After squats.",
    )
    page = logged_in.get(reverse("records:promote_symptom", args=[symptom.pk, "injury"]))
    assert page.context["form"].initial["since"] == date(2026, 11, 2)
    assert page.context["form"].initial["area"] == "knee"
    assert page.context["form"].initial["side"] == "left"

    response = logged_in.post(
        reverse("records:promote_symptom", args=[symptom.pk, "injury"]),
        {
            "area": "knee",
            "side": "left",
            "since": "2026-11-02",
            "since_precision": "day",
            "status": "active",
            "verification_status": "confirmed",
        },
    )
    assert response.status_code == 303
    injury = M.Injury._default_manager.get(user=user)
    assert injury.since == date(2026, 11, 2)
    assert M.Symptom._default_manager.filter(pk=symptom.pk).exists()

    response = logged_in.post(
        reverse("records:promote_symptom", args=[symptom.pk, "condition"]),
        {
            "name": "Knee pain",
            "since": "2026-11-02",
            "since_precision": "day",
            "status": "active",
            "verification_status": "confirmed",
        },
    )
    assert response.status_code == 303
    assert M.Condition._default_manager.get(user=user).name == "Knee pain"


def test_a_token_is_shown_once_and_only_its_digest_is_stored(user, logged_in):
    response = logged_in.post(reverse("records:tokens"), {"name": "health-mcp"})
    assert response.status_code == 200
    raw = response.context["new_token"]
    assert "new_token" not in logged_in.session
    assert all(raw not in str(value) for value in logged_in.session.items())
    assert raw.startswith("hi_")
    token = M.ApiToken._default_manager.get(user=user, name="health-mcp")
    assert token.prefix == raw[:12]
    assert token.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert all(raw not in str(value) for value in M.ApiToken._default_manager.values())

    again = logged_in.get(reverse("records:tokens"))
    assert raw not in again.content.decode()


def test_token_names_are_unique_while_active(user, logged_in):
    logged_in.post(reverse("records:tokens"), {"name": "health-mcp"})
    response = logged_in.post(reverse("records:tokens"), {"name": "health-mcp"})
    assert response.status_code == 200
    assert M.ApiToken._default_manager.filter(user=user, name="health-mcp").count() == 1
    token = M.ApiToken._default_manager.get(user=user)
    logged_in.post(reverse("records:token_revoke", args=[token.pk]))
    token.refresh_from_db()
    assert token.revoked_at is not None
    logged_in.post(reverse("records:tokens"), {"name": "health-mcp"})
    assert M.ApiToken._default_manager.filter(user=user, name="health-mcp").count() == 2


def test_audit_holds_no_health_values(user, logged_in):
    sentinel = "SENTINEL-STATEMENT"
    logged_in.post(
        reverse("records:memory_new"),
        {
            "kind": "fact",
            "subject": "training",
            "statement": sentinel,
            "observed_at": OBSERVED,
            "importance": "3",
        },
    )
    body = logged_in.get(reverse("records:audit")).content.decode()
    assert sentinel not in body
    assert all(sentinel not in str(event.detail) for event in M.AuditEvent._default_manager.all())
    assert M.AuditEvent._default_manager.filter(action="create").exists()


def test_tokens_are_owner_scoped(user, other_client):
    token, _ = M.ApiToken.issue(user, "health-mcp")
    assert other_client.get(reverse("records:tokens")).context["rows"].count() == 0
    response = other_client.post(reverse("records:token_revoke", args=[token.pk]))
    assert response.status_code == 404


def test_dashboard_lists_due_follow_ups(user, logged_in):
    entry(
        user,
        kind="follow_up",
        statement="Ask about the knee.",
        due_at=timezone.now(),
    )
    body = logged_in.get(reverse("records:dashboard")).content.decode()
    assert "Ask about the knee." in body


@pytest.mark.parametrize(
    "name",
    [
        "records:memory_list",
        "records:memory_new",
        "records:memory_review",
        "records:tokens",
        "records:settings",
        "records:audit",
    ],
)
def test_screens_render(name, logged_in):
    assert logged_in.get(reverse(name)).status_code == 200
