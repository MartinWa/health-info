"""Copy a verified backup into a new data directory and check the copy before it is used."""

import contextlib
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.loader import MigrationLoader

from records import backup

ALIAS = "restore"
LOCK_FILE = ".restore-lock"
MANIFEST_FIELDS = ("app_version", "counts")


def check_schema(database):
    """Ask the migration loader about the copy through a temporary alias, never about /data."""
    # The copy must be read by the backend the app boots with, so only the file differs.
    settings.DATABASES[ALIAS] = {**settings.DATABASES["default"], "NAME": str(database)}
    try:
        call_command("migrate", "--check", "--database", ALIAS)
        # --check only proves the copy is not behind; a newer build's history must be known too.
        loader = MigrationLoader(connections[ALIAS], ignore_no_migrations=True)
        unknown = set(loader.applied_migrations) - set(loader.disk_migrations)
        if unknown:
            named = ", ".join(sorted(f"{app}.{name}" for app, name in unknown))
            raise backup.BackupError(f"the copy carries migrations this build lacks: {named}")
    except SystemExit as exc:
        raise backup.BackupError(f"{database} is not at the schema this build expects") from exc
    finally:
        # Nested so a failing close cannot leave the alias behind in this process.
        try:
            connections[ALIAS].close()
        finally:
            del connections[ALIAS]
            settings.DATABASES.pop(ALIAS, None)


def contents(destination):
    """Names in the destination that are not this command's own lock file."""
    return [entry.name for entry in destination.iterdir() if entry.name != LOCK_FILE]


def reserve(destination, created):
    """Claim the destination for this run, so two restores cannot write into it at once."""
    if created:
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise backup.BackupError("it appeared after this run checked it") from exc
    lock = destination / LOCK_FILE
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError as exc:
        raise backup.BackupError(
            "another restore holds its lock file; delete it if nothing is running"
        ) from exc
    return lock


def check_copied(manifest, database):
    """Prove the copy is the bytes the manifest describes, not merely the same shape."""
    described = manifest["database"]
    copied = (database.stat().st_size, backup.sha256(database))
    if copied != (described["size"], described["sha256"]):
        raise backup.BackupError(f"{database} is not the copy the manifest describes")


def compare_counts(recorded, restored):
    """The manifest counts describe one snapshot; a difference means the copy is not it."""
    differing = [
        f"{label} {recorded.get(label, 0)} in the backup, {restored.get(label, 0)} in the copy"
        for label in sorted(set(recorded) | set(restored))
        if recorded.get(label) != restored.get(label)
    ]
    if differing:
        raise backup.BackupError("row counts differ: " + "; ".join(differing))


def release(destination, created, lock):
    """Let go of the claim without touching what is in it: a refusal deletes nothing."""
    lock.unlink(missing_ok=True)
    if created:
        with contextlib.suppress(OSError):
            destination.rmdir()


def discard(destination, created, lock):
    """Delete what this run wrote, releasing the lock last so no rival can start mid-cleanup."""
    for suffix in ("", "-wal", "-shm"):
        (destination / (backup.DATABASE_FILE + suffix)).unlink(missing_ok=True)
    lock.unlink(missing_ok=True)
    if created:
        with contextlib.suppress(OSError):
            destination.rmdir()


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
        missing = [field for field in MANIFEST_FIELDS if field not in manifest]
        if missing:
            raise CommandError(f"Refusing {source}: its manifest has no {', '.join(missing)}.")
        if destination == data or data in destination.parents:
            raise CommandError(f"Refusing {destination}: it overlaps the live data directory.")
        if destination.exists() and not destination.is_dir():
            raise CommandError(f"Refusing {destination}: it is not a directory.")
        # A lock file is this command's own mark, not content, so only other names are refused.
        if destination.is_dir() and contents(destination):
            raise CommandError(f"Refusing {destination}: it holds files. Restore into a new one.")
        created = not destination.exists()
        database = destination / backup.DATABASE_FILE
        try:
            lock = reserve(destination, created)
        except backup.BackupError as exc:
            raise CommandError(f"Refusing {destination}: {exc}") from exc
        # The claim decides: content that arrived after the check above is not written over,
        # and it is not this run's to delete - a faster restore may have just finished.
        if contents(destination):
            release(destination, created, lock)
            raise CommandError(f"Refusing {destination}: it holds files. Restore into a new one.")
        try:
            # Only the database: the destination is mounted at /data, where a manifest is not read.
            shutil.copy2(source / backup.DATABASE_FILE, database)
            check_copied(manifest, database)
            backup.check_copy(database)
            check_schema(database)
            restored = backup.counts(database)
            compare_counts(manifest["counts"], restored)
        except Exception as exc:
            discard(destination, created, lock)
            raise CommandError(f"Restore failed: {exc}") from exc
        lock.unlink(missing_ok=True)
        if manifest["app_version"] != settings.APP_VERSION:
            self.stderr.write(
                f"Warning: the backup was written by {manifest['app_version']} and this build "
                f"is {settings.APP_VERSION}; only pending migrations were checked."
            )
        self.stdout.write(
            f"{destination}: {sum(restored.values())} rows, "
            f"backup sha256 {manifest['database']['sha256'][:12]}, no pending migrations"
        )
