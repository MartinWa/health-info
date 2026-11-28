"""The rename's two leftovers: doses hang off their course by name, and permissions catch up.

Django renames the content type of a renamed model but leaves the four permission codenames
written by any earlier post_migrate, so an upgraded database carries add_period beside
add_event while a fresh one never has the old names. The second operation clears the retired
ones, which is why it lives in a new migration rather than inside 0006.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

RETIRED = {"period": "event", "doseevent": "dose", "auditevent": "auditentry"}
ACTIONS = ("add", "change", "delete", "view")


def rename_permissions(apps, pairs, alias):
    """Move each retired codename to its new name, dropping it only once its grants are safe."""
    Permission = apps.get_model("auth", "Permission")
    User = apps.get_model(settings.AUTH_USER_MODEL)
    Group = apps.get_model("auth", "Group")
    for old, new in pairs.items():
        for action in ACTIONS:
            for row in Permission.objects.using(alias).filter(codename=f"{action}_{old}"):
                taken = (
                    Permission.objects.using(alias)
                    .filter(content_type_id=row.content_type_id, codename=f"{action}_{new}")
                    .first()
                )
                if taken is None:
                    row.codename = f"{action}_{new}"
                    row.save(update_fields=["codename"])
                    continue
                # The new row already exists, so the old one is dropped - but it may be the one
                # a user or a group was granted, and deleting it takes those assignments with
                # it. Hand them over first.
                for user in User.objects.using(alias).filter(user_permissions=row):
                    user.user_permissions.add(taken)
                for group in Group.objects.using(alias).filter(permissions=row):
                    group.permissions.add(taken)
                row.delete()


def retire_permissions(apps, schema_editor):
    rename_permissions(apps, RETIRED, schema_editor.connection.alias)


def restore_permissions(apps, schema_editor):
    rename_permissions(
        apps, {new: old for old, new in RETIRED.items()}, schema_editor.connection.alias
    )


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0006_rename_auditevent_auditentry_rename_doseevent_dose_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dose",
            name="medication",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="doses",
                to="records.medication",
            ),
        ),
        migrations.RunPython(retire_permissions, restore_permissions),
    ]
