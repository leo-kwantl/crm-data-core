"""Pytest fixtures: ephemeral pgembed instance per session.

Applies db/migrations up-sections in filename order, exposes asyncpg pool
and httpx AsyncClient fixtures. Falls back gracefully when pgembed is missing
(tests that need a DB are skipped).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"

UP_RE = re.compile(r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)


def _up_sql(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = UP_RE.search(text)
    return m.group(1).strip() if m else ""


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    import os

    # DATABASE_URL swap path (task 7.1): point at managed/containerized PG to
    # run this same suite against it, e.g. DATABASE_URL=postgresql://... pytest.
    existing = os.environ.get("DATABASE_URL")
    if existing:
        return existing
    pgembed = pytest.importorskip("pgembed")
    server = pgembed.get_server(str(tmp_path_factory.mktemp("pgembed-test")))
    return server.get_uri("postgres")


@pytest_asyncio.fixture(scope="session")
async def pool(database_url: str) -> AsyncIterator:
    import asyncpg

    pg_pool = await asyncpg.create_pool(database_url)
    assert pg_pool is not None
    async with pg_pool.acquire() as conn:
        already = await conn.fetchval("SELECT to_regclass('public.customers')")
        if already is None:
            for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
                sql = _up_sql(mig)
                if sql:
                    try:
                        await conn.execute(sql)
                    except Exception as exc:
                        # Idempotent for combined multi-module runs where a
                        # function-scoped fixture already migrated this DB.
                        if "already exists" not in str(exc).lower():
                            raise
    yield pg_pool
    await pg_pool.close()


@pytest_asyncio.fixture()
async def client(pool) -> AsyncIterator:  # type: ignore[no-untyped-def]
    from httpx import ASGITransport, AsyncClient

    from services.api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True)
async def _seed_admin_session(database_url: str) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    """Seed the shared admin session (auth_helper.AUTH_H) before every test.

    Own short-lived pool per test: conftest's session pool may live on a
    different event loop than the test (see test_search.py note), so a fresh
    function-scoped pool keeps pool + test on ONE loop.
    """
    import asyncpg

    from services.api.tests.auth_helper import ensure_admin_session

    own = await asyncpg.create_pool(database_url, min_size=1, max_size=1)
    assert own is not None
    try:
        await ensure_admin_session(own)
    finally:
        await own.close()
    yield None
