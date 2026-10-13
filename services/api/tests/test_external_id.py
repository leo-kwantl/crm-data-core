"""External member ID tests: create/PATCH round-trip, uniqueness 409,
search-by-external hit, null allowed multiples, profile/list/detail shape."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


def _staff(tag: str) -> dict[str, str]:
    from services.api.tests.auth_helper import AUTH_H
    _ = tag
    return AUTH_H


@pytest_asyncio.fixture()
async def spool(database_url: str) -> AsyncIterator:  # type: ignore[no-untyped-def]
    import asyncpg

    p = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    assert p is not None
    async with p.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.customers')")
        if exists is None:
            up_re = re.compile(
                r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I
            )
            mig_dir = Path(__file__).resolve().parents[3] / "db" / "migrations"
            for mig in sorted(mig_dir.glob("*.sql")):
                m = up_re.search(mig.read_text(encoding="utf-8"))
                if m and m.group(1).strip():
                    await conn.execute(m.group(1).strip())
    yield p
    await p.close()


@pytest_asyncio.fixture()
async def ext_client(spool) -> AsyncIterator:  # type: ignore[no-untyped-def]
    from httpx import ASGITransport, AsyncClient

    from services.api import deps as deps_module
    from services.api.app import create_app

    app = create_app()

    async def _override_pool():
        yield spool

    app.dependency_overrides[deps_module.get_pool] = _override_pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset_limits():
    from services.api import deps as deps_module

    deps_module.reset_rate_limits()
    yield
    deps_module.reset_rate_limits()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _create(ext_client, tag: str, **kw) -> dict:
    body = {
        "full_name": f"Ext User {_uid()}",
        "contacts": [{"kind": "email", "value": f"ext-{_uid()}@example.com"}],
    }
    body.update(kw)
    r = await ext_client.post("/api/v1/customers", headers=_staff(tag), json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_with_external_id_round_trips(ext_client, spool) -> None:
    ext = f"POS-{_uid()}"
    created = await _create(ext_client, "create", external_member_id=ext)
    assert created["external_member_id"] == ext

    # detail read carries it
    r = await ext_client.get(f"/api/v1/customers/{created['id']}", headers=_staff("create"))
    assert r.status_code == 200
    assert r.json()["external_member_id"] == ext

    # list carries it
    r = await ext_client.get("/api/v1/customers?limit=100", headers=_staff("create"))
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["data"]}
    assert rows[created["id"]]["external_member_id"] == ext


async def test_duplicate_external_id_409(ext_client, spool) -> None:
    ext = f"POS-{_uid()}"
    await _create(ext_client, "dup1", external_member_id=ext)
    body = {
        "full_name": f"Dup User {_uid()}",
        "contacts": [{"kind": "email", "value": f"dup-{_uid()}@example.com"}],
        "external_member_id": ext,
    }
    r = await ext_client.post("/api/v1/customers", headers=_staff("dup2"), json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "duplicate_external_member_id"


async def test_patch_set_and_clear_external_id(ext_client, spool) -> None:
    created = await _create(ext_client, "patch")
    assert created["external_member_id"] is None
    ext = f"POS-{_uid()}"

    r = await ext_client.patch(
        f"/api/v1/customers/{created['id']}",
        headers=_staff("patch"),
        json={"external_member_id": ext},
    )
    assert r.status_code == 200, r.text
    assert r.json()["external_member_id"] == ext

    # blank clears back to NULL
    r = await ext_client.patch(
        f"/api/v1/customers/{created['id']}",
        headers=_staff("patch"),
        json={"external_member_id": ""},
    )
    assert r.status_code == 200, r.text
    assert r.json()["external_member_id"] is None

    # PATCH conflict -> 409
    await _create(ext_client, "patch2", external_member_id=ext)
    r = await ext_client.patch(
        f"/api/v1/customers/{created['id']}",
        headers=_staff("patch"),
        json={"external_member_id": ext},
    )
    assert r.status_code == 409


async def test_null_external_ids_allow_multiples(ext_client, spool) -> None:
    a = await _create(ext_client, "null1")
    b = await _create(ext_client, "null2")
    assert a["external_member_id"] is None
    assert b["external_member_id"] is None


async def test_search_by_external_id_hits(ext_client, spool) -> None:
    ext = f"POS-{_uid()}"
    created = await _create(ext_client, "search", external_member_id=ext)

    # POST body search via the member_id slot (internal OR external, exact only)
    r = await ext_client.post(
        "/api/v1/customers/search",
        headers=_staff("search"),
        json={"member_id": ext},
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["data"]]
    assert created["id"] in ids

    # GET ?member_id= lookup also hits the external id
    r = await ext_client.get(
        f"/api/v1/customers?member_id={ext}", headers=_staff("search")
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["data"]]
    assert created["id"] in ids

    # internal id still hits
    r = await ext_client.post(
        "/api/v1/customers/search",
        headers=_staff("search"),
        json={"member_id": created["member_id"]},
    )
    assert r.status_code == 200
    assert created["id"] in [row["id"] for row in r.json()["data"]]
