"""The retired-permission rename writes to the alias it is handed, never to "default"."""

import importlib

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType

rename_permissions = importlib.import_module(
    "records.migrations.0007_dose_related_name_and_retired_permissions"
).rename_permissions

ALIAS = "other"


def seed():
    """The retired "add_period" permission on ALIAS, granted to a user created there."""
    content_type = ContentType.objects.using(ALIAS).create(app_label="records", model="period")
    permission = Permission.objects.using(ALIAS).create(
        content_type=content_type, codename="add_period", name="Can add period"
    )
    user = get_user_model().objects.using(ALIAS).create(username="alice")
    user.user_permissions.add(permission)
    return content_type


def granted_codenames():
    user = get_user_model().objects.using(ALIAS).get(username="alice")
    return sorted(user.user_permissions.values_list("codename", flat=True))


def default_permissions():
    return sorted(Permission.objects.using("default").values_list("pk", "codename"))


@pytest.mark.django_db(databases=["default", ALIAS])
def test_the_rename_lands_on_the_alias_it_is_given():
    seed()
    before = default_permissions()

    rename_permissions(apps, {"period": "event"}, ALIAS)

    assert not Permission.objects.using(ALIAS).filter(codename="add_period").exists()
    assert granted_codenames() == ["add_event"]
    assert default_permissions() == before


@pytest.mark.django_db(databases=["default", ALIAS])
def test_the_hand_over_keeps_the_grant_on_the_alias_it_is_given():
    content_type = seed()
    Permission.objects.using(ALIAS).create(
        content_type=content_type, codename="add_event", name="Can add event"
    )
    before = default_permissions()

    rename_permissions(apps, {"period": "event"}, ALIAS)

    assert not Permission.objects.using(ALIAS).filter(codename="add_period").exists()
    assert granted_codenames() == ["add_event"]
    assert default_permissions() == before
