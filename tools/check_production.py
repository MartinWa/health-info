"""Verify the production image on disposable storage without external access."""

import http.client
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SECRET_KEY = "synthetic-production-check-key-never-for-runtime-0000000000"
PORT = 8000


def probe(path):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        connection.request("GET", path, headers={"Host": "127.0.0.1"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def wait_until_healthy(attempts=120):
    for _ in range(attempts):
        try:
            return probe("/healthz")[0]
        except OSError:
            time.sleep(0.5)
    raise AssertionError("The production server did not become healthy.")


def verify_running_as_non_root():
    if os.geteuid() == 0:
        raise AssertionError("The production image must run as a non-root user.")


def verify_dev_tooling_absent():
    for module in ("pip", "pytest"):
        assert importlib.util.find_spec(module) is None, module


def verify_version():
    expected = os.environ.get("EXPECTED_VERSION")
    if expected:
        from health_info.settings.base import APP_VERSION

        assert APP_VERSION == expected, (APP_VERSION, expected)


def verify_refuses_unsafe_secret():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HEALTH_INFO_SECRET_KEY", "HEALTH_INFO_DATA_DIR"))
    }
    result = subprocess.run(
        [sys.executable, "-c", "import health_info.settings.production"],
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0, "The production image started without a secret key."
    assert b"HEALTH_INFO_SECRET_KEY" in result.stderr, result.stderr


def verify_serving(data_directory):
    # health_info.server binds 0.0.0.0:8000 by design; nothing else runs in this check.
    child = subprocess.Popen(
        [sys.executable, "-m", "health_info.server"],
        env={
            **os.environ,
            "HEALTH_INFO_SECRET_KEY": SECRET_KEY,
            "HEALTH_INFO_ALLOWED_HOSTS": "127.0.0.1",
            "HEALTH_INFO_CSRF_TRUSTED_ORIGINS": "https://health.example.com",
            "HEALTH_INFO_DATA_DIR": str(data_directory),
            "HEALTH_INFO_MIGRATE_ON_START": "true",
        },
    )
    try:
        assert wait_until_healthy() == 200
        status, body = probe("/healthz")
        assert json.loads(body)["status"] == "ok", body
        root, _ = probe("/")
        assert root in (301, 302, 303), "Anonymous root must not render records."
    finally:
        child.terminate()
        child.wait(timeout=30)
    database = Path(data_directory) / "db.sqlite3"
    with sqlite3.connect(database) as connection:
        migrated = connection.execute("SELECT COUNT(*) FROM django_migrations").fetchone()[0]
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert migrated > 0, migrated
    assert journal == "wal", journal


def main():
    verify_running_as_non_root()
    verify_dev_tooling_absent()
    verify_refuses_unsafe_secret()
    verify_version()
    verify_serving(Path(tempfile.mkdtemp(prefix="health-info-production-check-")))
    print("Verified non-root startup, automatic migrations and HTTP serving.")


if __name__ == "__main__":
    main()
