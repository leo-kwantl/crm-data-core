"""Local dev/test Postgres boot via pgembed.

Thin wrapper: returns a DATABASE_URL pointing at an embedded PG17 instance.
Managed PG (Neon/Supabase/RDS) is used in pilot/prod via the same env var —
no SQL changes. Pin major version via EMBEDDED_PG_VERSION if needed.
"""

from __future__ import annotations

import os

_cached_url: str | None = None


def get_database_url(data_dir: str | None = None) -> str:
    """Return DATABASE_URL, booting pgembed when the env var is absent."""
    global _cached_url
    existing = os.environ.get("DATABASE_URL")
    if existing:
        return existing
    if _cached_url:
        return _cached_url
    from pgembed import get_server  # lazy: keeps import light without pgembed installed

    server = get_server(data_dir or ".pgembed-data")
    url = server.get_uri("postgres")
    # pgembed returns sync-style URL; normalize driver for asyncpg/pg drivers.
    _cached_url = url
    os.environ.setdefault("DATABASE_URL", url)
    return url
