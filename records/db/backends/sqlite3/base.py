import sqlite3

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper

from health_info.runtime import wal_runtime_is_fixed


class DatabaseWrapper(SQLiteDatabaseWrapper):
    def get_new_connection(self, conn_params):
        if not wal_runtime_is_fixed(sqlite3.sqlite_version_info):
            raise ImproperlyConfigured("SQLite lacks the WAL-reset fix; use the Linux container.")
        connection = super().get_new_connection(conn_params)
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise ImproperlyConfigured("SQLite requires file-backed, host-local WAL storage.")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
        except Exception:
            connection.close()
            raise
        return connection
