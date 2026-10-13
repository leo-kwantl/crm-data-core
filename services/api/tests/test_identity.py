"""Identity module tests: staff CRUD + delete-blocked-409, users CRUD +
disable toggle + DELETE-405 + disabled-read-200, linkage constraints,
unlinked-staff picker, cursor paging, auth-stub 401, no-store headers."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

from services.api.tests.auth_helper import AUTH_H as STAFF_H
@pytest_asyncio.fixture()
async def spool(database_url: str) -> AsyncIterator:  # type: ignore[no-untyped-def]
    import asyncpg

    p = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    assert p is not None
    async with p.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.staff')")
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
async def ident_client(spool) -> AsyncIterator:  # type: ignore[no-untyped-def]
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


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _mk_staff(ident_client, tag: str = "x", **kw) -> dict:
    body = {"staff_code": f"T-{_uid()}", "full_name": f"Test Staff {_uid()}"}
    body.update(kw)
    r = await ident_client.post("/api/v1/staff", headers=STAFF_H, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _mk_user(ident_client, **kw) -> dict:
    body = {"username": f"u-{_uid()}", "display_name": f"User {_uid()}"}
    body.update(kw)
    r = await ident_client.post("/api/v1/users", headers=STAFF_H, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_staff_crud_round_trip(ident_client, spool) -> None:
    s = await _mk_staff(ident_client)
    rid = s["id"]
    assert s["left_at"] is None

    r = await ident_client.get(f"/api/v1/staff/{rid}", headers=STAFF_H)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"

    r = await ident_client.patch(
        f"/api/v1/staff/{rid}", headers=STAFF_H, json={"full_name": "Renamed Staff"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Renamed Staff"

    r = await ident_client.get("/api/v1/staff?limit=5", headers=STAFF_H)
    assert r.status_code == 200
    assert any(row["id"] == rid for row in r.json()["data"])

    r = await ident_client.delete(f"/api/v1/staff/{rid}", headers=STAFF_H)
    assert r.status_code == 204
    r = await ident_client.get(f"/api/v1/staff/{rid}", headers=STAFF_H)
    assert r.status_code == 404


async def test_staff_duplicate_code_409(ident_client, spool) -> None:
    code = f"T-{_uid()}"
    await _mk_staff(ident_client, staff_code=code)
    r = await ident_client.post(
        "/api/v1/staff",
        headers=STAFF_H,
        json={"staff_code": code, "full_name": "Dupe"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "duplicate_staff_code"


async def test_staff_delete_blocked_when_linked_409(ident_client, spool) -> None:
    s = await _mk_staff(ident_client)
    await _mk_user(ident_client, staff_id=s["id"])
    r = await ident_client.delete(f"/api/v1/staff/{s['id']}", headers=STAFF_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "staff_has_users"
    # staff row still readable
    r = await ident_client.get(f"/api/v1/staff/{s['id']}", headers=STAFF_H)
    assert r.status_code == 200


async def test_staff_delete_after_unlink_204(ident_client, spool) -> None:
    s = await _mk_staff(ident_client)
    u = await _mk_user(ident_client, staff_id=s["id"])
    r = await ident_client.patch(
        f"/api/v1/users/{u['id']}", headers=STAFF_H, json={"clear_staff": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["staff_id"] is None
    r = await ident_client.delete(f"/api/v1/staff/{s['id']}", headers=STAFF_H)
    assert r.status_code == 204


async def test_user_crud_disable_and_deleted_never(ident_client, spool) -> None:
    u = await _mk_user(ident_client)
    uid = u["id"]
    assert u["is_disabled"] is False

    r = await ident_client.patch(
        f"/api/v1/users/{uid}", headers=STAFF_H, json={"is_disabled": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_disabled"] is True

    # disabled-user read still 200
    r = await ident_client.get(f"/api/v1/users/{uid}", headers=STAFF_H)
    assert r.status_code == 200
    assert r.json()["is_disabled"] is True

    # re-enable
    r = await ident_client.patch(
        f"/api/v1/users/{uid}", headers=STAFF_H, json={"is_disabled": False}
    )
    assert r.status_code == 200
    assert r.json()["is_disabled"] is False

    # NO hard delete
    for path in ("/api/v1/users", f"/api/v1/users/{uid}"):
        r = await ident_client.delete(path, headers=STAFF_H)
        assert r.status_code == 405, path


async def test_user_duplicate_username_409(ident_client, spool) -> None:
    name = f"u-{_uid()}"
    await _mk_user(ident_client, username=name)
    r = await ident_client.post(
        "/api/v1/users",
        headers=STAFF_H,
        json={"username": name, "display_name": "Dupe"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "duplicate_username"


async def test_user_staff_link_constraints(ident_client, spool) -> None:
    # link to missing staff -> 422
    r = await ident_client.post(
        "/api/v1/users",
        headers=STAFF_H,
        json={
            "username": f"u-{_uid()}",
            "display_name": "Ghost",
            "staff_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "staff_not_found"

    # one staff -> one user: second link -> 409
    s = await _mk_staff(ident_client)
    await _mk_user(ident_client, staff_id=s["id"])
    r = await ident_client.post(
        "/api/v1/users",
        headers=STAFF_H,
        json={"username": f"u-{_uid()}", "display_name": "Second", "staff_id": s["id"]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "staff_already_linked"

    # nullable link: unlinked user is fine
    u = await _mk_user(ident_client)
    assert u["staff_id"] is None


async def test_unlinked_staff_picker(ident_client, spool) -> None:
    s = await _mk_staff(ident_client)
    r = await ident_client.get("/api/v1/users/unlinked-staff", headers=STAFF_H)
    assert r.status_code == 200
    assert s["id"] in [row["id"] for row in r.json()["data"]]
    await _mk_user(ident_client, staff_id=s["id"])
    r = await ident_client.get("/api/v1/users/unlinked-staff", headers=STAFF_H)
    assert s["id"] not in [row["id"] for row in r.json()["data"]]


async def test_identity_lists_page_and_require_staff(ident_client, spool) -> None:
    for _ in range(3):
        await _mk_staff(ident_client)
    r = await ident_client.get("/api/v1/staff?limit=2", headers=STAFF_H)
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 2
    assert body["next_cursor"]
    r2 = await ident_client.get(
        f"/api/v1/staff?limit=2&cursor={body['next_cursor']}", headers=STAFF_H
    )
    assert r2.status_code == 200
    assert r2.headers.get("cache-control") == "no-store"

    r = await ident_client.get("/api/v1/users?limit=2", headers=STAFF_H)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"

    # stub identity still required (except the public staff roster for sign-in)
    r = await ident_client.get("/api/v1/staff")
    assert r.status_code == 200
    for path in ("/api/v1/users",):
        r = await ident_client.get(path)
        assert r.status_code == 401, path
