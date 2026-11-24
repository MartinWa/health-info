"""Labs panels, export, purge, search and llms.txt."""

import io
import json
import zipfile
from datetime import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from records import models as M


def lab(user, **overrides):
    values = {
        "user": user,
        "panel": "Metabolic",
        "analyte": "Glucose",
        "value": "5.2",
        "unit": "mmol/L",
        "collected_at": timezone.make_aware(datetime(2026, 11, 20, 8, 0)),
    }
    values.update(overrides)
    return M.Lab._default_manager.create(**values)


def test_a_panel_form_saves_every_analyte_in_one_post(user, logged_in):
    response = logged_in.post(
        reverse("records:labs_new"),
        {
            "panel": "Metabolic",
            "collected_at": "2026-11-20T08:00",
            "lab_name": "Example lab",
            "fasting": "true",
            "analyte-TOTAL_FORMS": "2",
            "analyte-INITIAL_FORMS": "0",
            "analyte-MIN_NUM_FORMS": "0",
            "analyte-MAX_NUM_FORMS": "1000",
            "analyte-0-analyte": "Glucose",
            "analyte-0-value": "5.2",
            "analyte-0-unit": "mmol/L",
            "analyte-1-analyte": "HbA1c",
            "analyte-1-value": "38",
            "analyte-1-unit": "mmol/mol",
        },
    )
    assert response.status_code == 303
    rows = M.Lab._default_manager.filter(user=user).order_by("analyte")
    assert [row.analyte for row in rows] == ["Glucose", "HbA1c"]
    assert {row.panel for row in rows} == {"Metabolic"}
    assert {row.lab_name for row in rows} == {"Example lab"}
    assert rows[0].fasting is True


def test_a_panel_with_no_analyte_is_refused(user, logged_in):
    response = logged_in.post(
        reverse("records:labs_new"),
        {
            "panel": "Metabolic",
            "collected_at": "2026-11-20T08:00",
            "analyte-TOTAL_FORMS": "1",
            "analyte-INITIAL_FORMS": "0",
            "analyte-MIN_NUM_FORMS": "0",
            "analyte-MAX_NUM_FORMS": "1000",
        },
    )
    assert response.status_code == 200
    assert not M.Lab._default_manager.filter(user=user).exists()


def test_new_panel_prefills_from_the_last_panel_with_the_same_name(user, logged_in):
    lab(user, ref_low="4.0", ref_high="6.0")
    response = logged_in.get(reverse("records:labs_new") + "?panel=Metabolic")
    formset = response.context["formset"]
    initial = formset.forms[0].initial
    assert initial["analyte"] == "Glucose"
    assert initial["unit"] == "mmol/L"
    assert initial["ref_low"] == Decimal("4")
    assert response.context["panel_form"].initial["panel"] == "Metabolic"


def test_prefilled_rows_that_are_left_blank_do_not_block_the_panel(user, logged_in):
    lab(user)
    lab(user, analyte="HbA1c", unit="mmol/mol", value="38")
    response = logged_in.post(
        reverse("records:labs_new"),
        {
            "panel": "Metabolic",
            "collected_at": "2026-11-27T08:00",
            "analyte-TOTAL_FORMS": "2",
            "analyte-INITIAL_FORMS": "2",
            "analyte-MIN_NUM_FORMS": "0",
            "analyte-MAX_NUM_FORMS": "1000",
            "analyte-0-analyte": "Glucose",
            "analyte-0-unit": "mmol/L",
            "analyte-0-value": "5.6",
            "analyte-1-analyte": "HbA1c",
            "analyte-1-unit": "mmol/mol",
        },
    )
    assert response.status_code == 303
    new_at = timezone.make_aware(datetime(2026, 11, 27, 8, 0))
    saved = M.Lab._default_manager.filter(user=user, collected_at=new_at)
    assert [(row.analyte, row.value) for row in saved] == [("Glucose", Decimal("5.6"))]


def test_an_analyte_row_without_a_value_says_so(user, logged_in):
    response = logged_in.post(
        reverse("records:labs_new"),
        {
            "panel": "Metabolic",
            "collected_at": "2026-11-20T08:00",
            "analyte-TOTAL_FORMS": "1",
            "analyte-INITIAL_FORMS": "0",
            "analyte-MIN_NUM_FORMS": "0",
            "analyte-MAX_NUM_FORMS": "1000",
            "analyte-0-analyte": "Glucose",
        },
    )
    assert response.status_code == 200
    assert not M.Lab._default_manager.filter(user=user).exists()
    body = response.content.decode()
    assert "A result needs a number or a text value" in body


def test_labs_edit_and_delete_of_another_account_are_404(user, other_client):
    row = lab(user)
    assert other_client.get(reverse("records:labs_edit", args=[row.pk])).status_code == 404
    assert other_client.post(reverse("records:labs_delete", args=[row.pk])).status_code == 404
    assert M.Lab._default_manager.filter(pk=row.pk).exists()


def test_labs_list_shows_no_panel_from_another_account(user, other_client):
    lab(user)
    response = other_client.get(reverse("records:labs_list"))
    assert response.status_code == 200
    assert response.context["panels"] == []


def test_labs_list_groups_rows_into_panels(user, logged_in):
    lab(user)
    lab(user, analyte="HbA1c")
    response = logged_in.get(reverse("records:labs_list"))
    assert response.status_code == 200
    assert [panel["count"] for panel in response.context["panels"]] == [2]


def test_panel_detail_shows_only_its_own_collection(user, logged_in):
    first = lab(user)
    lab(user, analyte="HbA1c")
    lab(user, panel="Lipids", analyte="LDL")
    response = logged_in.get(reverse("records:labs_detail", args=[first.pk]))
    assert [row["analyte"] for row in response.context["rows"]] == ["Glucose", "HbA1c"]


def test_another_accounts_panel_is_404(user, other_client):
    response = other_client.get(reverse("records:labs_detail", args=[lab(user).pk]))
    assert response.status_code == 404


def test_a_saved_result_can_be_corrected_and_deleted(user, logged_in):
    row = lab(user)
    response = logged_in.post(
        reverse("records:labs_edit", args=[row.pk]),
        {
            "panel": "Metabolic",
            "analyte": "Glucose",
            "value": "5.9",
            "unit": "mmol/L",
            "collected_at": "2026-11-20T08:00",
        },
    )
    assert response.status_code == 303
    assert M.Lab._default_manager.get(pk=row.pk).value == Decimal("5.9")
    assert logged_in.post(reverse("records:labs_delete", args=[row.pk])).status_code == 303
    assert not M.Lab._default_manager.filter(user=user).exists()


def test_editing_one_result_keeps_the_panel_metadata_consistent(user, logged_in):
    row = lab(user)
    sibling = lab(user, analyte="HbA1c", lab_name="Old lab", fasting=False)
    other_panel = lab(user, panel="Lipids", analyte="LDL", lab_name="Old lab")
    response = logged_in.post(
        reverse("records:labs_edit", args=[row.pk]),
        {
            "panel": "Metabolic",
            "analyte": "Glucose",
            "value": "5.9",
            "unit": "mmol/L",
            "collected_at": "2026-11-20T08:00",
            "lab_name": "New lab",
            "fasting": "true",
        },
    )
    assert response.status_code == 303
    group = M.Lab._default_manager.filter(user=user, panel="Metabolic")
    assert {(row.lab_name, row.fasting) for row in group} == {("New lab", True)}
    assert M.Lab._default_manager.get(pk=sibling.pk).lab_name == "New lab"
    assert M.Lab._default_manager.get(pk=other_panel.pk).lab_name == "Old lab"


def test_export_is_a_zip_of_only_the_signed_in_account(user, stranger, logged_in):
    lab(user)
    M.Note._default_manager.create(user=user, body="own note", at=timezone.now())
    M.Note._default_manager.create(user=stranger, body="sentinel-other", at=timezone.now())
    _, secret = M.ApiToken.issue(user, "health-mcp")
    response = logged_in.get(reverse("records:export"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        payload = json.loads(archive.read("health-info-export.json"))
    body = json.dumps(payload)
    assert "sentinel-other" not in body
    assert "own note" in body
    assert payload["tables"]["records.Lab"][0]["analyte"] == "Glucose"
    assert payload["tables"]["records.Lab"][0]["created_at"]
    assert "records.ApiToken" not in payload["tables"]
    assert secret not in body
    assert "key_hash" not in body


def test_purge_needs_the_word_and_keeps_other_accounts_rows(user, stranger, logged_in):
    lab(user)
    M.DoseEvent._default_manager.create(
        user=user,
        medication=M.Medication._default_manager.create(
            user=user, substance="Candesartan", schedule={"type": "daily"}
        ),
        status="taken",
        at=timezone.now(),
    )
    M.Note._default_manager.create(user=stranger, body="sentinel-other", at=timezone.now())

    assert logged_in.post(reverse("records:purge"), {"confirm": "no"}).status_code == 303
    assert M.Lab._default_manager.filter(user=user).exists()

    assert logged_in.post(reverse("records:purge"), {"confirm": "purge"}).status_code == 303
    assert not M.Lab._default_manager.filter(user=user).exists()
    assert not M.Medication._default_manager.filter(user=user).exists()
    assert M.Note._default_manager.filter(user=stranger).count() == 1


def test_purge_keeps_tokens_and_the_audit_trail(user, logged_in):
    lab(user)
    token, _ = M.ApiToken.issue(user, "health-mcp")
    assert logged_in.post(reverse("records:purge"), {"confirm": "purge"}).status_code == 303
    assert not M.Lab._default_manager.filter(user=user).exists()
    assert M.ApiToken._default_manager.filter(pk=token.pk).exists()
    assert M.AuditEvent._default_manager.filter(user=user, action="purge").exists()


def test_purge_removes_a_memory_correction_chain(user, logged_in):
    old = M.MemoryEntry._default_manager.create(
        user=user, kind="fact", subject="knee", statement="Hurts", observed_at=timezone.now()
    )
    M.MemoryEntry._default_manager.create(
        user=user,
        kind="correction",
        subject="knee",
        statement="Better",
        observed_at=timezone.now(),
        corrects=old,
    )
    assert logged_in.post(reverse("records:purge"), {"confirm": "purge"}).status_code == 303
    assert not M.MemoryEntry._default_manager.filter(user=user).exists()


def test_search_finds_own_rows_only(user, stranger, logged_in):
    M.Note._default_manager.create(user=user, body="squat felt heavy", at=timezone.now())
    M.Note._default_manager.create(user=stranger, body="squat sentinel-other", at=timezone.now())
    response = logged_in.get(reverse("records:search"), {"q": "squat"})
    assert response.status_code == 200
    assert [row["text"] for row in response.context["results"]] == ["squat felt heavy"]


def test_search_covers_notes_outcome_notes_and_the_programme(user, logged_in):
    M.Allergy._default_manager.create(
        user=user, substance="Pollen", note="worst in birch season", source="client"
    )
    M.Appointment._default_manager.create(
        user=user,
        at=timezone.now(),
        practitioner_name="Dr Example",
        outcome_note="sentinel-outcome",
    )
    M.Program._default_manager.create(user=user, name="sentinel-programme 5x5")
    for query, expected in (
        ("birch", "Pollen"),
        ("sentinel-outcome", "Dr Example 2026-11-24"),
        ("sentinel-programme", "sentinel-programme 5x5"),
    ):
        response = logged_in.get(reverse("records:search"), {"q": query})
        assert [row["text"] for row in response.context["results"]] == [expected], query


def test_search_offers_a_continuation_past_ten_matches(user, logged_in):
    for index in range(11):
        M.Note._default_manager.create(user=user, body=f"squat note {index}", at=timezone.now())
    first = logged_in.get(reverse("records:search"), {"q": "squat"})
    assert len(first.context["results"]) == 10
    assert [row["label"] for row in first.context["more"]] == ["notes"]

    response = logged_in.get(first.context["more"][0]["url"])
    assert [row["text"] for row in response.context["results"]] == ["squat note 0"]
    assert response.context["back"].endswith("offset=0")
    assert response.context["all_url"] == reverse("records:search") + "?q=squat"


@pytest.mark.parametrize("query", ["", "zzz-no-such-text"])
def test_search_without_a_hit_renders(query, logged_in):
    response = logged_in.get(reverse("records:search"), {"q": query})
    assert response.status_code == 200
    assert response.context["results"] == []


def test_llms_txt_is_plain_text_for_a_signed_in_account(logged_in, user):
    response = logged_in.get(reverse("records:llms"))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    body = response.content.decode()
    assert "/api/v1" in body
    assert "Bearer" in body


def test_the_new_screens_need_a_session(client):
    for name in ("records:labs_list", "records:labs_new", "records:export", "records:purge"):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert response["Location"].startswith("/login/")
    assert client.get(reverse("records:llms")).status_code == 302


def test_the_dashboard_lab_line_links_to_labs(user, logged_in):
    lab(user)
    response = logged_in.get(reverse("records:dashboard"))
    labs = next(line for line in response.context["lines"] if line["domain"] == "labs")
    assert labs["url"] == reverse("records:labs_list")
    assert labs["state"] == "present"
