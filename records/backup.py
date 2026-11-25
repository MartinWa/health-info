"""One VACUUM INTO copy, a manifest, and the checks that prove both.

Nothing here copies a live database by file: `cp` of a WAL database is a torn read.
"""

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import connection

DATABASE_FILE = "db.sqlite3"
MANIFEST_FILE = "manifest.json"
LISTED_FILES = (DATABASE_FILE, MANIFEST_FILE)
KEEP_DAILY = 14
KEEP_MONTHLY = 12


class BackupError(Exception):
    """A backup or restore step was refused; the message says which."""


def counts(database=None):
    """Rows per health model, in a stable order: from the copy when a copy is named."""
    models = sorted(
        (model for model in apps.get_models() if model._meta.app_label == "records"),
        key=lambda model: model._meta.label,
    )
    if database is None:
        return {model._meta.label: model._default_manager.count() for model in models}
    with contextlib.closing(connect(database)) as probe:
        return {
            model._meta.label: probe.execute(
                f"select count(*) from {model._meta.db_table}"
            ).fetchone()[0]
            for model in models
        }


def connect(database):
    """A read-only handle on a copy. as_uri() escapes the # and ? that would otherwise end
    the URI, so mode=ro is never dropped and no stray file is created."""
    return sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def vacuum_into(destination):
    """Write a consistent copy. SQLite overwrites an empty file silently, so the caller
    must have refused an existing path first."""
    with connection.cursor() as cursor:
        cursor.execute("VACUUM INTO %s", [str(destination)])


def check_copy(database):
    """integrity_check and foreign_key_check on the copy, never on the live database."""
    with contextlib.closing(connect(database)) as probe:
        integrity = probe.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise BackupError(f"the copy fails integrity_check: {integrity[:3]}")
        broken = probe.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise BackupError(f"the copy fails foreign_key_check: {len(broken)} broken references")


def write_manifest(directory, database):
    manifest = {
        "app_version": settings.APP_VERSION,
        "image": os.environ.get("HEALTH_INFO_IMAGE", ""),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": {
            "file": DATABASE_FILE,
            "sha256": sha256(database),
            "size": database.stat().st_size,
        },
        "counts": counts(database),  # the copy, so the manifest describes one snapshot
    }
    (directory / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify(directory):
    """Prove a backup directory holds exactly the manifest and an unmodified copy."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError(f"{directory} is not a plain directory")
    for entry in directory.iterdir():
        if entry.is_symlink() or entry.name not in LISTED_FILES:
            raise BackupError(f"{entry} is not part of this backup")
    if {entry.name for entry in directory.iterdir()} != set(LISTED_FILES):
        raise BackupError(f"{directory} is missing {MANIFEST_FILE} or {DATABASE_FILE}")
    try:
        manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
        created_at = manifest["created_at"]
        if not isinstance(created_at, str):
            raise ValueError("created_at is not a string")
        datetime.fromisoformat(created_at)
        described = manifest["database"]
        if described["file"] != DATABASE_FILE:
            raise BackupError(f"{directory} points its manifest at {described['file']}")
        database = directory / DATABASE_FILE
        if database.stat().st_size != described["size"]:
            raise BackupError(f"{database} does not match its manifest size")
        if sha256(database) != described["sha256"]:
            raise BackupError(f"{database} does not match its manifest sha256")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"{directory} has an unreadable manifest: {exc}") from exc


def prune(backups):
    """Keep the newest 14 and the newest of each of the last 12 months, never the newest.

    A directory without a valid manifest is neither counted nor deleted: it is not ours.
    """
    dated = []
    for entry in Path(backups).iterdir():
        try:
            dated.append((verify(entry)["created_at"], entry))
        except BackupError:
            continue
    dated.sort(reverse=True)
    if not dated:
        return []
    keep = {entry for _, entry in dated[:KEEP_DAILY]}
    months = {}
    for created_at, entry in dated:
        months.setdefault(created_at[:7], entry)  # newest per month; newest first
    keep.update(list(months.values())[:KEEP_MONTHLY])
    keep.add(dated[0][1])
    removed = [entry for _, entry in dated if entry not in keep]
    for entry in removed:
        shutil.rmtree(entry)
    return removed
