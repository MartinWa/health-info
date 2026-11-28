"""File-backed WAL tests isolated from development and production data."""

import os
from pathlib import Path

from .base import *  # noqa: F403
from .base import BASE_DIR, DATABASES

# The container keeps test state on its writable volume; the bind-mounted checkout is not
# writable by the unprivileged container user.
TEST_DATA_DIR = Path(os.environ.get("HEALTH_INFO_TEST_DATA_DIR") or BASE_DIR / ".data/test-state")
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASES["default"]["NAME"] = TEST_DATA_DIR / "db.sqlite3"
DATABASES["default"]["TEST"] = {"NAME": TEST_DATA_DIR / "test.sqlite3"}
DATABASES["other"] = {
    **DATABASES["default"],
    "NAME": TEST_DATA_DIR / "other.sqlite3",
    "TEST": {"NAME": TEST_DATA_DIR / "test-other.sqlite3"},
}
SECRET_KEY = "synthetic-test-secret-not-for-real-installations-" * 2
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
STATIC_ROOT = TEST_DATA_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
