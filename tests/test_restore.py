"""Restore: a verified backup becomes a data directory, or nothing is left behind."""

import contextlib
import io
import json
import sqlite3

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import setup_databases, teardown_databases

from records import backup
from records.management.commands.restore_health_info import LOCK_FILE


@pytest.fixture
def live(django_db_setup, django_db_blocker):
    """The command opens a second alias, which only the unblocked database allows."""
    with django_db_blocker.unblock():
        if connection.introspection.table_names():
            yield
            return
        # A run that collects no database test builds nothing, so build the session database.
        built = setup_databases(verbosity=0, interactive=False, aliases={"default"})
        try:
            yield
        finally:
            teardown_databases(built, verbosity=0)


@pytest.fixture
def source(live, tmp_path):
    """A real backup of that database, taken with the command that writes them."""
    destination = tmp_path / "backups" / "2026-11-24T0310"
    destination.parent.mkdir()
    call_command("backup_health_info", str(destination))
    return destination


def rewrite_manifest(directory):
    """Keep the manifest honest after a test edits the copy, so verify() still passes."""
    database = directory / backup.DATABASE_FILE
    manifest = json.loads((directory / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["database"]["size"] = database.stat().st_size
    manifest["database"]["sha256"] = backup.sha256(database)
    (directory / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")


def test_a_backup_restores_into_a_new_directory(tmp_path, source):
    destination = tmp_path / "restored"
    output = io.StringIO()

    call_command("restore_health_info", str(source), str(destination), stdout=output)

    assert (destination / backup.DATABASE_FILE).is_file()
    assert not (destination / backup.MANIFEST_FILE).exists()
    assert not (destination / LOCK_FILE).exists()
    assert backup.counts(destination / backup.DATABASE_FILE) == backup.verify(source)["counts"]
    assert output.getvalue().endswith("no pending migrations\n")


def test_an_existing_empty_directory_is_filled(tmp_path, source):
    destination = tmp_path / "restored"
    destination.mkdir()

    call_command("restore_health_info", str(source), str(destination))

    assert (destination / backup.DATABASE_FILE).is_file()


def test_refuses_a_destination_inside_the_data_directory(tmp_path, source):
    destination = settings.DATA_DIR / "restored"

    with pytest.raises(CommandError, match="live data directory"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def test_refuses_a_destination_that_holds_files_and_leaves_them_alone(tmp_path, source):
    destination = tmp_path / "restored"
    destination.mkdir()
    (destination / "keep.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(CommandError, match="holds files"):
        call_command("restore_health_info", str(source), str(destination))
    assert [entry.name for entry in destination.iterdir()] == ["keep.txt"]


def test_refuses_a_destination_that_another_restore_holds(tmp_path, source):
    destination = tmp_path / "restored"
    destination.mkdir()
    (destination / LOCK_FILE).write_text("", encoding="utf-8")

    with pytest.raises(CommandError, match="another restore holds its lock file"):
        call_command("restore_health_info", str(source), str(destination))
    assert [entry.name for entry in destination.iterdir()] == [LOCK_FILE]


def test_a_failure_in_an_existing_empty_directory_leaves_it_empty(tmp_path, source):
    notes = backup.counts(source / backup.DATABASE_FILE)["records.Note"]
    manifest = json.loads((source / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["counts"]["records.Note"] = notes + 1
    (source / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(CommandError, match="row counts differ"):
        call_command("restore_health_info", str(source), str(destination))
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_refuses_a_manifest_without_an_app_version(tmp_path, source):
    manifest = json.loads((source / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    del manifest["app_version"]
    (source / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"

    with pytest.raises(CommandError, match="no app_version"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def test_refuses_a_copy_that_is_not_the_bytes_the_manifest_describes(tmp_path, source, monkeypatch):
    """A source swapped between the hash and the copy is caught by hashing the copy."""
    swapped = backup.verify(source)
    swapped["database"]["sha256"] = "0" * 64
    monkeypatch.setattr(backup, "verify", lambda directory: swapped)
    destination = tmp_path / "restored"

    with pytest.raises(CommandError, match="not the copy the manifest describes"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def test_refuses_a_copy_carrying_a_migration_this_build_does_not_know(tmp_path, source):
    database = source / backup.DATABASE_FILE
    with contextlib.closing(sqlite3.connect(database)) as copy:
        copy.execute(
            "insert into django_migrations (app, name, applied) values (?, ?, datetime('now'))",
            ["records", "9999_from_a_newer_build"],
        )
        copy.commit()
    rewrite_manifest(source)
    destination = tmp_path / "restored"

    with pytest.raises(CommandError, match="records.9999_from_a_newer_build"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def test_refuses_a_destination_that_is_a_file_and_leaves_it_alone(tmp_path, source):
    destination = tmp_path / "restored"
    destination.write_text("mine", encoding="utf-8")

    with pytest.raises(CommandError, match="not a directory"):
        call_command("restore_health_info", str(source), str(destination))
    assert destination.read_text(encoding="utf-8") == "mine"


def test_refuses_a_tampered_backup_and_writes_nothing(tmp_path, source):
    with open(source / backup.DATABASE_FILE, "ab") as handle:
        handle.write(b"tampered")
    destination = tmp_path / "restored"

    with pytest.raises(CommandError, match="does not match"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def test_refuses_counts_that_disagree_with_the_copy(tmp_path, source):
    notes = backup.counts(source / backup.DATABASE_FILE)["records.Note"]
    manifest = json.loads((source / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["counts"]["records.Note"] = 99
    (source / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"

    with pytest.raises(CommandError, match=rf"records\.Note 99 in the backup, {notes} in the copy"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def drop_one_applied_leaf(database):
    """Delete one leaf migration's row, so the copy is exactly one behind and still coherent."""
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    applied = loader.applied_migrations
    leaf = sorted(node for node in loader.graph.leaf_nodes() if node in applied)[-1]
    with contextlib.closing(sqlite3.connect(database)) as copy:
        copy.execute("delete from django_migrations where app = ? and name = ?", list(leaf))
        copy.commit()


def test_refuses_a_copy_that_is_one_migration_behind(tmp_path, source):
    drop_one_applied_leaf(source / backup.DATABASE_FILE)
    rewrite_manifest(source)
    destination = tmp_path / "restored"

    with pytest.raises(CommandError, match="schema"):
        call_command("restore_health_info", str(source), str(destination))
    assert not destination.exists()


def test_warns_when_the_backup_came_from_another_build(tmp_path, source):
    manifest = json.loads((source / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["app_version"] = "0.6.0"
    (source / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"
    errors = io.StringIO()

    call_command("restore_health_info", str(source), str(destination), stderr=errors)

    assert errors.getvalue().startswith("Warning: the backup was written by 0.6.0")
