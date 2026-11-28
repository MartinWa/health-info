"""Copy a verified backup into a new data directory and check the copy before it is used."""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from records import backup

ALIAS = "restore"


def check_schema(database):
    """Ask the migration loader about the copy through a temporary alias, never about /data."""
    # The copy must be read by the backend the app boots with, so only the file differs.
    settings.DATABASES[ALIAS] = {**settings.DATABASES["default"], "NAME": str(database)}
    try:
        call_command("migrate", "--check", "--database", ALIAS)
    except SystemExit as exc:
        raise backup.BackupError(f"{database} is not at the schema this build expects") from exc
    finally:
        # Nested so a failing close cannot leave the alias behind in this process.
        try:
            connections[ALIAS].close()
        finally:
            del connections[ALIAS]
            settings.DATABASES.pop(ALIAS, None)


def compare_counts(recorded, restored):
    """The manifest counts describe one snapshot; a difference means the copy is not it."""
    differing = [
        f"{label} {recorded.get(label, 0)} in the backup, {restored.get(label, 0)} in the copy"
        for label in sorted(set(recorded) | set(restored))
        if recorded.get(label) != restored.get(label)
    ]
    if differing:
        raise backup.BackupError("row counts differ: " + "; ".join(differing))


def discard(destination, created):
    """Remove only what this run wrote: the directory it made, or the copy it added to one."""
    if created:
        shutil.rmtree(destination, ignore_errors=True)
        return
    for suffix in ("", "-wal", "-shm"):
        (destination / (backup.DATABASE_FILE + suffix)).unlink(missing_ok=True)


class Command(BaseCommand):
    help = "Copy a verified backup into a new data directory and check the copy."

    def add_arguments(self, parser):
        parser.add_argument("source", help="A backup directory written by backup_health_info")
        parser.add_argument(
            "destination", help="A new or empty directory to become HEALTH_INFO_DATA_DIR"
        )

    def handle(self, *args, **options):
        source = Path(options["source"]).resolve()
        destination = Path(options["destination"]).resolve()
        data = Path(settings.DATA_DIR).resolve()
        try:
            manifest = backup.verify(source)
        except backup.BackupError as exc:
            raise CommandError(f"Refusing {source}: {exc}") from exc
        if destination == data or data in destination.parents:
            raise CommandError(f"Refusing {destination}: it overlaps the live data directory.")
        if destination.exists() and not destination.is_dir():
            raise CommandError(f"Refusing {destination}: it is not a directory.")
        if destination.exists() and any(destination.iterdir()):
            raise CommandError(f"Refusing {destination}: it holds files. Restore into a new one.")
        created = not destination.exists()
        database = destination / backup.DATABASE_FILE
        try:
            destination.mkdir(mode=0o700, exist_ok=True)
            # Only the database: the destination is mounted at /data, where a manifest is not read.
            shutil.copy2(source / backup.DATABASE_FILE, database)
            backup.check_copy(database)
            check_schema(database)
            restored = backup.counts(database)
            compare_counts(manifest["counts"], restored)
        except Exception as exc:
            discard(destination, created)
            raise CommandError(f"Restore failed: {exc}") from exc
        if manifest["app_version"] != settings.APP_VERSION:
            self.stderr.write(
                f"Warning: the backup was written by {manifest['app_version']} and this build "
                f"is {settings.APP_VERSION}; only pending migrations were checked."
            )
        self.stdout.write(
            f"{destination}: {sum(restored.values())} rows, "
            f"backup sha256 {manifest['database']['sha256'][:12]}, no pending migrations"
        )
