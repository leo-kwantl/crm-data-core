"""Connector write/trigger endpoint tests (data D1, HTTP-level).

Lifecycle: create (201 + has_secret-only shape) -> test (stored +
prospective) -> sync trigger (202 queued) -> secret rotation (no
round-trip) -> delete (204 + cascade). Plus 409/404/422/403 edges and
the D1 guarantees: embedded secrets rejected, secret values never
echoed anywhere, disabled connector is a kill-switch (no force).
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from services.api.tests.auth_helper import AUTH_H as ADMIN_H
from services.api.tests.auth_helper import make_session

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def db_pool(database_url: str):
    """Function-loop pool; migrates idempotently (mirrors test_orders)."""
    import re

    import asyncpg

    own = await asyncpg.create_pool(database_url)
    async with own.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.customers')")
        if exists is None:
            from pathlib import Path
            up_re = re.compile(
                r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)",
                re.S | re.I)
            mig_dir = (Path(__file__).resolve().parents[3]
                       / "db" / "migrations")
            for mig in sorted(mig_dir.glob("*.sql")):
                m = up_re.search(mig.read_text(encoding="utf-8"))
                if m and m.group(1).strip():
                    try:
                        await conn.execute(m.group(1).strip())
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
    yield own
    await own.close()


@pytest_asyncio.fixture()
async def client(db_pool) -> AsyncIterator:  # type: ignore[no-untyped-def]
    from httpx import ASGITransport, AsyncClient

    from services.api import deps as deps_mod
    from services.api.app import create_app

    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_pool

    app.dependency_overrides[deps_mod.get_pool] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport,
                           base_url="http://test") as ac:
        yield ac


async def _create(client, payload: dict[str, Any],
                  headers: dict[str, str] = ADMIN_H) -> Any:
    return await client.post("/api/v1/connectors", json=payload,
                             headers=headers)


async def test_create_validate_and_shape(client, db_pool):
    r = await _create(client, {
        "source_type": "shopify", "name": "flagship",
        "config": {"shop_domain": "demo.myshopify.com"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_type"] == "shopify"
    assert body["config"] == {"shop_domain": "demo.myshopify.com"}
    assert body["secret_ref"] is None
    assert body["has_secret"] is False
    assert body["enabled"] is True


async def test_create_duplicate_409(client):
    payload = {"source_type": "csv", "name": "pos-export",
               "config": {"column_map": {"member": "member_id"}}}
    assert (await _create(client, payload)).status_code == 201
    r = await _create(client, payload)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "duplicate_connector"


async def test_create_rejects_embedded_secret_and_reserved(client):
    r = await _create(client, {
        "source_type": "shopify", "name": "leaky",
        "config": {"shop_domain": "d", "api_token": "shpat-X"}})
    assert r.status_code == 422
    r = await _create(client, {
        "source_type": "woocommerce", "name": "woo",
        "config": {"shop_domain": "d"}})
    assert r.status_code == 422
    r = await _create(client, {
        "source_type": "shopify", "name": "missing-keys", "config": {}})
    assert r.status_code == 422


async def test_test_endpoint_stored_and_prospective(client, monkeypatch):
    r = await _create(client, {
        "source_type": "rest_api", "name": "erp",
        "config": {"base_url": "https://erp.example.com"},
        "secret_ref": "TEST_CONNECTOR_SECRET_XYZ"})
    cid = r.json()["id"]
    # stored: valid config, secret absent.
    t = await client.post(f"/api/v1/connectors/{cid}/test",
                          headers=ADMIN_H)
    assert t.status_code == 200, t.text
    body = t.json()
    assert body["config_valid"] is True
    assert body["secret_present"] is False
    # prospective override: bad config flagged, nothing persisted.
    t2 = await client.post(
        f"/api/v1/connectors/{cid}/test",
        json={"source_type": "shopify", "config": {}},
        headers=ADMIN_H)
    assert t2.json()["config_valid"] is False
    assert t2.json()["config_error"] is not None
    # secret presence detected without ever echoing a value.
    monkeypatch.setenv("TEST_CONNECTOR_SECRET_XYZ", "s3cr3t")
    t3 = await client.post(f"/api/v1/connectors/{cid}/test",
                           headers=ADMIN_H)
    assert t3.json()["secret_present"] is True
    assert "s3cr3t" not in t3.text


async def test_sync_trigger_enqueue_and_kill_switch(client):
    r = await _create(client, {
        "source_type": "csv", "name": "nightly-csv",
        "config": {"column_map": {"member": "member_id"}}})
    cid = r.json()["id"]
    s = await client.post(f"/api/v1/connectors/{cid}/sync",
                          json={"entity": "orders"}, headers=ADMIN_H)
    assert s.status_code == 202, s.text
    assert s.json()["status"] == "queued"
    assert s.json()["entity"] == "orders"
    bad = await client.post(
        f"/api/v1/connectors/{cid}/sync",
        json={"entity": "nope"}, headers=ADMIN_H)
    assert bad.status_code == 422
    # disabled connector: 422, no force flag.
    off = await _create(client, {
        "source_type": "csv", "name": "paused",
        "config": {"column_map": {}}, "enabled": False})
    s2 = await client.post(
        f"/api/v1/connectors/{off.json()['id']}/sync",
        json={"entity": "orders"}, headers=ADMIN_H)
    assert s2.status_code == 422


async def test_secret_rotation_never_round_trips(client, monkeypatch):
    r = await _create(client, {
        "source_type": "shopify", "name": "rot",
        "config": {"shop_domain": "d.myshopify.com"},
        "secret_ref": "TEST_ROTATE_SECRET_XYZ"})
    cid = r.json()["id"]
    monkeypatch.delenv("TEST_ROTATE_SECRET_XYZ", raising=False)
    rot = await client.put(f"/api/v1/connectors/{cid}/secret",
                           json={"value": "new-secret-value"},
                           headers=ADMIN_H)
    assert rot.status_code == 200, rot.text
    body = rot.json()
    assert body["has_secret"] is True
    assert "new-secret-value" not in rot.text
    assert os.environ.get("TEST_ROTATE_SECRET_XYZ") == "new-secret-value"
    # rotation without a secret_ref: 422.
    plain = await _create(client, {
        "source_type": "csv", "name": "no-ref",
        "config": {"column_map": {}}})
    r2 = await client.put(
        f"/api/v1/connectors/{plain.json()['id']}/secret",
        json={"value": "x"}, headers=ADMIN_H)
    assert r2.status_code == 422


async def test_delete_cascades_runs(client, db_pool):
    r = await _create(client, {
        "source_type": "csv", "name": "gone",
        "config": {"column_map": {}}})
    cid = r.json()["id"]
    await client.post(f"/api/v1/connectors/{cid}/sync",
                      json={"entity": "orders"}, headers=ADMIN_H)
    d = await client.delete(f"/api/v1/connectors/{cid}", headers=ADMIN_H)
    assert d.status_code == 204
    async with db_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM connector_sync_runs"
            " WHERE connector_id = $1", cid)
        assert int(n) == 0
    again = await client.delete(f"/api/v1/connectors/{cid}",
                                headers=ADMIN_H)
    assert again.status_code == 404


async def test_all_writes_admin_only(client, db_pool):
    staff = await make_session(db_pool, "connector-staff", role="manager")
    r = await _create(client, {
        "source_type": "csv", "name": "nope",
        "config": {"column_map": {}}}, headers=staff)
    assert r.status_code == 403
    # admin creates one so the sub-paths exist for the 403 checks.
    c = await _create(client, {
        "source_type": "csv", "name": "gated",
        "config": {"column_map": {}}})
    cid = c.json()["id"]
    for method, path, payload in [
        ("delete", f"/api/v1/connectors/{cid}", None),
        ("post", f"/api/v1/connectors/{cid}/sync",
         {"entity": "orders"}),
        ("post", f"/api/v1/connectors/{cid}/test", {}),
        ("put", f"/api/v1/connectors/{cid}/secret",
         {"value": "x"}),
    ]:
        resp = await client.request(method, path, json=payload,
                                    headers=staff)
        assert resp.status_code == 403, (method, path, resp.text)


# ---- merged reads (app-mirrored; staff-gated, SQL-backed) ----

async def test_reads_list_detail_runs_staff_gated(client, db_pool):
    staff = await make_session(db_pool, "connector-reader", role="manager")
    r = await _create(client, {
        "source_type": "csv", "name": "readable",
        "config": {"column_map": {}}})
    cid = r.json()["id"]
    await client.post(f"/api/v1/connectors/{cid}/sync",
                      json={"entity": "orders"}, headers=ADMIN_H)
    lst = await client.get("/api/v1/connectors", headers=staff)
    assert lst.status_code == 200, lst.text
    items = lst.json()["connectors"]
    assert any(i["id"] == cid for i in items)
    mine = next(i for i in items if i["id"] == cid)
    assert mine["last_run"] is not None
    assert mine["last_run"]["status"] == "queued"
    det = await client.get(f"/api/v1/connectors/{cid}", headers=staff)
    assert det.status_code == 200
    assert det.json()["name"] == "readable"
    runs = await client.get(
        f"/api/v1/connectors/{cid}/sync-runs", headers=staff)
    assert runs.status_code == 200
    assert runs.json()["sync_runs"][0]["entity"] == "orders"
    filt = await client.get(
        f"/api/v1/connectors/{cid}/sync-runs?entity=nope", headers=staff)
    assert filt.status_code == 422


async def test_reads_404_code(client):
    r = await client.get(
        "/api/v1/connectors/00000000-0000-0000-0000-000000000000",
        headers=ADMIN_H)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "connector_not_found"


# ---- converged semantics (app rulings 2026-10-15) ----

async def test_reads_has_secret_is_env_live(client, monkeypatch):
    r = await _create(client, {
        "source_type": "csv", "name": "live-bit",
        "config": {"column_map": {}},
        "secret_ref": "TEST_LIVE_BIT_XYZ"})
    cid = r.json()["id"]
    monkeypatch.delenv("TEST_LIVE_BIT_XYZ", raising=False)
    # ref configured but env unset -> False on BOTH reads and writes.
    det = await client.get(f"/api/v1/connectors/{cid}", headers=ADMIN_H)
    assert det.json()["has_secret"] is False
    assert det.json()["secret_ref"] == "TEST_LIVE_BIT_XYZ"
    lst = await client.get("/api/v1/connectors", headers=ADMIN_H)
    mine = next(i for i in lst.json()["connectors"] if i["id"] == cid)
    assert mine["has_secret"] is False
    # env set -> True everywhere, value never echoed.
    monkeypatch.setenv("TEST_LIVE_BIT_XYZ", "v")
    det2 = await client.get(f"/api/v1/connectors/{cid}", headers=ADMIN_H)
    assert det2.json()["has_secret"] is True
    assert det2.json()["secret_ref"] == "TEST_LIVE_BIT_XYZ"


async def test_write_404_code_aligned(client):
    r = await client.get(
        "/api/v1/connectors/00000000-0000-0000-0000-000000000001/sync",
        headers=ADMIN_H)
    assert r.status_code == 405  # GET not mounted; POST below is the check
    p = await client.post(
        "/api/v1/connectors/00000000-0000-0000-0000-000000000001/sync",
        json={"entity": "orders"}, headers=ADMIN_H)
    assert p.status_code == 404
    assert p.json()["detail"]["code"] == "connector_not_found"
