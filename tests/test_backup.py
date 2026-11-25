"""Backups: refuse what would lose or hide data, and prove what was written."""

import contextlib
import json
import os
import sqlite3

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError

from records import backup


def a_backup(directory, created_at):
    """A directory shaped like a backup, with a real (tiny) database inside it."""
    directory.mkdir(parents=True)
    database = directory / backup.DATABASE_FILE
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("create table t (id integer)")
        connection.execute("insert into t values (1)")
        connection.commit()
    (directory / backup.MANIFEST_FILE).write_text(
        json.dumps(
            {
                "app_version": "test",
                "created_at": created_at,
                "counts": {},
                "database": {
                    "file": backup.DATABASE_FILE,
                    "sha256": backup.sha256(database),
                    "size": database.stat().st_size,
                },
                "image": "",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return directory


@pytest.mark.django_db(transaction=True)
def test_a_backup_is_a_verified_copy_with_the_live_counts(tmp_path, user, make_row):
    make_row("notes")
    make_row("checkins")
    destination = tmp_path / "backups" / "2026-11-24T0310"
    destination.parent.mkdir()

    call_command("backup_health_info", str(destination))

    manifest = backup.verify(destination)
    assert manifest["counts"] == backup.counts()
    assert manifest["counts"]["records.Note"] == 1
    assert manifest["counts"]["records.Checkin"] == 1
    with contextlib.closing(sqlite3.connect(destination / backup.DATABASE_FILE)) as copy:
        assert copy.execute("select count(*) from records_note").fetchone()[0] == 1
        assert copy.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.django_db(transaction=True)
def test_refuses_an_existing_destination_and_leaves_it_alone(tmp_path):
    target = tmp_path / "2026-11-24T0310"
    target.mkdir()
    with pytest.raises(CommandError, match="exists"):
        call_command("backup_health_info", str(target))
    assert list(target.iterdir()) == []


@pytest.mark.django_db(transaction=True)
def test_refuses_a_destination_inside_the_data_directory(tmp_path):
    with pytest.raises(CommandError, match="cannot live inside"):
        call_command("backup_health_info", str(settings.DATA_DIR / "backups" / "2026-11-24T0310"))
    assert not (settings.DATA_DIR / "backups").exists()


@pytest.mark.django_db(transaction=True)
def test_refuses_a_missing_parent_directory(tmp_path):
    with pytest.raises(CommandError, match="is not a directory"):
        call_command("backup_health_info", str(tmp_path / "nowhere" / "2026-11-24T0310"))


@pytest.mark.django_db(transaction=True)
def test_manifest_counts_describe_the_copy_not_the_live_database(tmp_path, user, make_row):
    make_row("notes")
    destination = tmp_path / "2026-11-24T0310"
    destination.mkdir()
    database = destination / backup.DATABASE_FILE
    backup.vacuum_into(database)
    make_row("notes")  # written after the copy was taken

    manifest = backup.write_manifest(destination, database)

    assert manifest["counts"]["records.Note"] == 1
    assert backup.counts()["records.Note"] == 2


@pytest.mark.django_db(transaction=True)
def test_a_destination_name_with_a_hash_is_checked_where_it_lives(tmp_path, user, make_row):
    make_row("notes")
    destination = tmp_path / "snapshot#1"

    call_command("backup_health_info", str(destination))

    assert backup.verify(destination)["counts"]["records.Note"] == 1
    assert [entry.name for entry in tmp_path.iterdir()] == ["snapshot#1"]  # no stray file


@pytest.mark.django_db(transaction=True)
def test_vacuum_into_refuses_a_database_but_overwrites_an_empty_file(tmp_path):
    taken = tmp_path / "taken.sqlite3"
    backup.vacuum_into(taken)
    with pytest.raises(DatabaseError):
        backup.vacuum_into(taken)

    empty = tmp_path / "empty.sqlite3"
    empty.touch()
    backup.vacuum_into(empty)  # silent: the reason the command refuses an existing path
    assert empty.stat().st_size > 0


def test_verify_rejects_a_tampered_copy(tmp_path):
    directory = a_backup(tmp_path / "2026-11-24T0310", "2026-11-24T03:10:00+00:00")
    with open(directory / backup.DATABASE_FILE, "ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(backup.BackupError, match="does not match"):
        backup.verify(directory)


def test_verify_rejects_an_unlisted_file(tmp_path):
    directory = a_backup(tmp_path / "2026-11-24T0310", "2026-11-24T03:10:00+00:00")
    (directory / "notes.txt").write_text("unlisted", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="not part of this backup"):
        backup.verify(directory)


def test_verify_rejects_a_symlink(tmp_path):
    directory = a_backup(tmp_path / "2026-11-24T0310", "2026-11-24T03:10:00+00:00")
    try:
        os.symlink(directory / backup.DATABASE_FILE, directory / "link.sqlite3")
    except OSError:  # a Windows checkout without symlink privilege
        pytest.skip("this filesystem does not allow symlinks")
    with pytest.raises(backup.BackupError, match="not part of this backup"):
        backup.verify(directory)


def test_verify_rejects_a_manifest_pointing_elsewhere(tmp_path):
    directory = a_backup(tmp_path / "2026-11-24T0310", "2026-11-24T03:10:00+00:00")
    manifest = json.loads((directory / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["database"]["file"] = "../elsewhere.sqlite3"
    (directory / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="points its manifest at"):
        backup.verify(directory)


def test_verify_rejects_a_manifest_without_a_timestamp(tmp_path):
    directory = a_backup(tmp_path / "2026-11-24T0310", "2026-11-24T03:10:00+00:00")
    manifest = json.loads((directory / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["created_at"] = None
    (directory / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="created_at"):
        backup.verify(directory)


def test_prune_survives_a_manifest_without_a_timestamp(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    broken = a_backup(backups / "broken", "2026-11-24T03:10:00+00:00")
    manifest = json.loads((broken / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["created_at"] = None
    (broken / backup.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    kept = a_backup(backups / "kept", "2026-11-25T03:10:00+00:00")

    assert backup.prune(backups) == []
    assert broken.exists()
    assert kept.exists()


def test_prune_keeps_14_daily_and_12_monthly_and_never_the_newest(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for day in range(1, 21):
        a_backup(backups / f"2026-11-{day:02d}T0310", f"2026-11-{day:02d}T03:10:00+00:00")
    older = [f"2025-{month:02d}-01" for month in range(11, 13)] + [
        f"2026-{month:02d}-01" for month in range(1, 11)
    ]
    for day in older:
        a_backup(backups / f"{day}T0310", f"{day}T03:10:00+00:00")

    removed = backup.prune(backups)

    kept = sorted(entry.name for entry in backups.iterdir())
    assert len(kept) == 14 + 11, kept
    assert "2026-11-20T0310" in kept
    assert "2026-11-07T0310" in kept
    assert "2026-11-06T0310" not in kept
    assert "2025-12-01T0310" in kept
    assert "2025-11-01T0310" not in kept
    assert len(removed) == 7


def test_prune_ignores_a_directory_without_a_valid_manifest(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    newer = a_backup(backups / "newer", "2026-11-24T03:10:00+00:00")
    stranger = backups / "hand-copied"
    stranger.mkdir()
    (stranger / "db.sqlite3").write_bytes(b"not ours")

    removed = backup.prune(backups)

    assert removed == []
    assert newer.exists()
    assert stranger.exists()
