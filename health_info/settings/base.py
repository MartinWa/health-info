"""Shared configuration; installation values belong in the private environment."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("HEALTH_INFO_DATA_DIR", BASE_DIR / ".data"))


def env_bool(name, default=False):
    value = os.environ.get(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ImproperlyConfigured(f"{name} must be true or false.")
    return value in {"true", "1"}


def env_list(name):
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def env_text(name, default=""):
    """Read a value from the environment, or from a file named by its NAME_FILE companion."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    if not path:
        return default
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ImproperlyConfigured(f"Read {name}_FILE {path}: {exc}") from exc


INSTALLED_APPS = [
    "health_info.admin_site.HealthInfoAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "records.apps.RecordsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "records.middleware.ApiGuardMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "records.middleware.AccountTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "health_info.urls"
WSGI_APPLICATION = "health_info.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "health_info" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
DATABASES = {
    "default": {
        "ENGINE": "records.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 10, "transaction_mode": "IMMEDIATE"},
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": 0,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "records:dashboard"
LOGOUT_REDIRECT_URL = "login"
LANGUAGE_CODE = "en"
TIME_ZONE = os.environ.get("HEALTH_INFO_TIME_ZONE", "Europe/Stockholm")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
# PDFs are served only by owner-scoped views, never by static or media routing.
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
try:
    MAX_UPLOAD_BYTES = int(os.environ.get("HEALTH_INFO_MAX_UPLOAD_BYTES", 20 * 1024 * 1024))
except ValueError as exc:
    raise ImproperlyConfigured("HEALTH_INFO_MAX_UPLOAD_BYTES must be an integer.") from exc
if not 1 <= MAX_UPLOAD_BYTES <= 100 * 1024 * 1024:
    raise ImproperlyConfigured("HEALTH_INFO_MAX_UPLOAD_BYTES must be between 1 and 104857600.")
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_BYTES + 64 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 1
SESSION_COOKIE_NAME = "__Host-health_info_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
MIGRATE_ON_START = env_bool("HEALTH_INFO_MIGRATE_ON_START", True)
MIGRATION_LOCK_TIMEOUT = 300
APP_VERSION = os.environ.get("HEALTH_INFO_VERSION", "development").strip() or "development"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
