"""The rename's two leftovers: doses hang off their course by name, and permissions catch up.

Django renames the content type of a renamed model but leaves the four permission codenames
written by any earlier post_migrate, so an upgraded database carries add_period beside
add_event while a fresh one never has the old names. The second operation clears the retired
ones, which is why it lives in a new migration rather than inside 0006.
"""

import django.db.models.deletion
from django.db import migrations, models

RETIRED = {"period": "event", "doseevent": "dose", "auditevent": "auditentry"}
ACTIONS = ("add", "change", "delete", "view")


def rename_permissions(apps, pairs):
    """Move each retired codename to its new name, or drop it when the new one is already there."""
    Permission = apps.get_model("auth", "Permission")
    for old, new in pairs.items():
        for action in ACTIONS:
            for row in Permission.objects.filter(codename=f"{action}_{old}"):
                taken = Permission.objects.filter(
                    content_type_id=row.content_type_id, codename=f"{action}_{new}"
                )
                if taken.exists():
                    row.delete()
                else:
                    row.codename = f"{action}_{new}"
                    row.save(update_fields=["codename"])


def retire_permissions(apps, schema_editor):
    rename_permissions(apps, RETIRED)


def restore_permissions(apps, schema_editor):
    rename_permissions(apps, {new: old for old, new in RETIRED.items()})


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
