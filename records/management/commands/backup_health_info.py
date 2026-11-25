"""Write one verified backup; the nightly task must alert on a non-zero exit."""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from records import backup


class Command(BaseCommand):
    help = "Write one verified backup into a new directory and prune older ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "destination", help="A new directory under /backups, e.g. /backups/2026-11-24T0310"
        )

    def handle(self, *args, **options):
        destination = Path(options["destination"]).resolve()
        data = Path(settings.DATA_DIR).resolve()
        if destination == data or data in destination.parents:
            raise CommandError(f"Refusing {destination}: a backup cannot live inside {data}.")
        if destination.exists():
            raise CommandError(f"Refusing {destination}: it exists. Use a new timestamped name.")
        if not destination.parent.is_dir():
            raise CommandError(f"Refusing {destination}: {destination.parent} is not a directory.")
        database = destination / backup.DATABASE_FILE
        destination.mkdir(mode=0o700)
        try:
            backup.vacuum_into(database)
            backup.check_copy(database)
            backup.write_manifest(destination, database)
            manifest = backup.verify(destination)
        except Exception as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise CommandError(f"Backup failed: {exc}") from exc
        removed = backup.prune(destination.parent)
        rows = sum(manifest["counts"].values())
        self.stdout.write(
            f"{destination}: {rows} rows, sha256 {manifest['database']['sha256'][:12]}, "
            f"pruned {len(removed)}"
        )
