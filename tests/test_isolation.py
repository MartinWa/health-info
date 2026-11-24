"""Each account's data is only returned to that account: another id is 404, never 403."""

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from records import models as M
from records import views as V


@pytest.mark.parametrize("slug", sorted(V.RESOURCES))
def test_another_accounts_id_is_404_for_every_verb(slug, user, other_client, make_row):
    obj = make_row(slug)
    for name in ("detail", "edit", "delete"):
        url = reverse(f"records:{slug}_{name}", args=[obj.pk])
        method = other_client.post if name == "delete" else other_client.get
        assert method(url).status_code == 404, (slug, name)
    assert V.RESOURCES[slug]["model"]._default_manager.filter(user__username="bob").count() == 0


@pytest.mark.parametrize("slug", sorted(V.RESOURCES))
def test_collections_return_no_rows_from_another_account(slug, other_client, make_row):
    make_row(slug)
    response = other_client.get(reverse(f"records:{slug}_list"))
    assert response.status_code == 200
    assert response.context["rows"] == []
    assert "sentinel" not in response.content.decode().lower()


@pytest.mark.parametrize("slug", sorted(V.RESOURCES))
def test_the_owner_does_see_their_own_row(slug, logged_in, make_row):
    obj = make_row(slug)
    assert logged_in.get(reverse(f"records:{slug}_detail", args=[obj.pk])).status_code == 200
    listing = logged_in.get(reverse(f"records:{slug}_list"))
    assert len(listing.context["rows"]) == 1


@pytest.mark.parametrize("slug", sorted(V.RESOURCES))
def test_new_and_edit_forms_render(slug, logged_in, make_row):
    obj = make_row(slug)
    assert logged_in.get(reverse(f"records:{slug}_new")).status_code == 200
    assert logged_in.get(reverse(f"records:{slug}_edit", args=[obj.pk])).status_code == 200


def test_person_singleton_never_shows_another_accounts_name(user, other_client):
    M.Person._default_manager.create(user=user, name="sentinel-name")
    response = other_client.get(reverse("records:person"))
    assert response.status_code == 200
    assert "sentinel-name" not in response.content.decode()
    # A GET renders the form; it does not create a row or mark the domain present.
    assert not M.Person._default_manager.filter(user__username="bob").exists()
    assert M.domain_state(M.Person._default_manager.get(user=user).user, "identity") == "present"


def test_dashboard_counts_only_the_signed_in_account(other_client, make_row):
    make_row("conditions")
    body = other_client.get(reverse("records:dashboard")).content.decode()
    assert "sentinel-condition" not in body


def test_signed_out_users_are_sent_to_login(client):
    for name in ("records:dashboard", "records:quick", "records:timeline", "records:regimen"):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert response["Location"].startswith("/login/")


def test_admin_holds_no_health_model():
    assert set(admin.site._registry) == {get_user_model(), Group, M.ApiToken}


def test_views_have_no_bare_default_manager_calls():
    source = open(V.__file__, encoding="utf-8").read()
    assert ".objects." not in source
