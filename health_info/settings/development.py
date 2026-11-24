"""Local development settings; never used in the container."""

from .base import *  # noqa: F403
from .base import DATA_DIR

DEBUG = True
SECRET_KEY = "development-only-secret-key-not-for-real-installations-0123456789"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Browsers reject a __Host- cookie without Secure, so local HTTP uses the plain name.
SESSION_COOKIE_NAME = "health_info_session"
