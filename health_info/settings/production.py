"""Private HTTPS deployment; require explicit installation configuration."""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import DATA_DIR, env_bool, env_list, env_text

SECRET_KEY = env_text("HEALTH_INFO_SECRET_KEY")
ALLOWED_HOSTS = env_list("HEALTH_INFO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("HEALTH_INFO_CSRF_TRUSTED_ORIGINS")
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("REPLACE"):
    raise ImproperlyConfigured(
        "Set a random HEALTH_INFO_SECRET_KEY of at least 50 characters, or point "
        "HEALTH_INFO_SECRET_KEY_FILE at a Docker or Podman secret."
    )
if not ALLOWED_HOSTS or any("*" in host or "://" in host for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured(
        "Set explicit HEALTH_INFO_ALLOWED_HOSTS without schemes or wildcards."
    )
if not os.environ.get("HEALTH_INFO_DATA_DIR") or not DATA_DIR.is_absolute():
    raise ImproperlyConfigured("Set HEALTH_INFO_DATA_DIR to absolute, private host-local storage.")
if any(not origin.startswith("https://") or "*" in origin for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("HEALTH_INFO_CSRF_TRUSTED_ORIGINS must contain exact HTTPS origins.")
# The container healthcheck calls /healthz over loopback, so it cannot be a public host and
# must not be redirected to HTTPS.
ALLOWED_HOSTS = [*ALLOWED_HOSTS, "127.0.0.1", "localhost"]
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
if env_bool("HEALTH_INFO_TRUST_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
