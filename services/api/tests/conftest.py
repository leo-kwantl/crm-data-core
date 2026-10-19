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
def database_url(
    tmp_path_factory: pytest.TempPathFactory, pytestconfig: pytest.Config
) -> str:
    import os

    # DATABASE_URL swap path: point at managed/containerized PG to run this
    # same suite against it, e.g. DATABASE_URL=postgresql://... pytest.
    # Opt-in only: an externally supplied URL is somebody else's database, and
    # this suite leaves rows behind by design (see tests/shared_db_guard.py).
    existing = os.environ.get("DATABASE_URL")
    if existing:
        from services.api.tests import shared_db_guard

        problem = shared_db_guard.shared_url_problem(dict(os.environ))
        if problem:
            pytest.exit(problem, returncode=4)
        # Opted in: remember what the database looked like so the session can
        # prove it was left that way.
        import asyncio

        import asyncpg

        async def _counts() -> dict[str, int]:
            conn = await asyncpg.connect(existing)
            try:
                return await shared_db_guard.table_counts(conn)
            finally:
                await conn.close()

        pytestconfig._shared_dsn = existing  # type: ignore[attr-defined]
        pytestconfig._shared_before = asyncio.run(_counts())  # type: ignore[attr-defined]
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


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a shared-database run that changed the row count of any table.

    Only armed when the run opted into `DATABASE_URL` (+ `CRM_CDP_ALLOW_SHARED_DB=1`);
    the default throwaway cluster is not audited because tests write freely
    there. A drifting table means some fixture wrote and did not clean up --
    the failure names the tables so the next reader does not have to hunt.
    """
    dsn = getattr(session.config, "_shared_dsn", None)
    before = getattr(session.config, "_shared_before", None)
    if not dsn or not before:
        return

    import asyncio

    import asyncpg

    from services.api.tests import shared_db_guard

    async def _after() -> dict[str, int]:
        conn = await asyncpg.connect(dsn)
        try:
            return await shared_db_guard.table_counts(conn)
        finally:
            await conn.close()

    moved = shared_db_guard.diff(before, asyncio.run(_after()))
    if moved:
        session.exitstatus = 1
        print(
            "\nshared database left dirty (row counts): "
            + shared_db_guard.describe(moved)
            + "\n  a fixture wrote and did not delete its rows; clean up in a"
            " `finally`, or unset DATABASE_URL to run on a throwaway cluster."
        )
