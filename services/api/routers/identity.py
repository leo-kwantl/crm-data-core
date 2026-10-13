"""Identity endpoints: staff roster + login-account records + password admin.

Auth is session-based (routers/auth.py issues tokens; deps.get_current_user
guards). Only GET /staff (the roster list) stays public; every other route
here needs a session. Role gates (deps.require_admin, 403 otherwise):
user create/update/password-reset and staff create/delete are admin-only —
manager/sa get all data but no account administration. Backend 403 is the
real guard; frontend role-hiding is best-effort only.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..deps import CurrentUser, get_current_user, get_pool, get_session_token, require_admin
from ..repos import auth as auth_repo
from ..repos import identity as repo
from ..repos.paging import clamp_page_size
from ..schemas.auth import PasswordSetIn
from ..schemas.identity import StaffCreate, StaffUpdate, UserCreate, UserUpdate
from .auth import MIN_PASSWORD_LEN

router = APIRouter(tags=["identity"])


def _dup(exc: Exception, code: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "value": str(exc)})


# --- staff ---

@router.get("/staff")
async def list_staff(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    # Public: the roster stays readable pre-login (as before the auth cutover).
    if page is not None:
        return await repo.list_staff_offset(
            pool, page=page, page_size=clamp_page_size(page_size), q=q
        )
    try:
        items, next_cursor = await repo.list_staff_page(pool, limit=limit, cursor=cursor, q=q)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {"data": items, "next_cursor": next_cursor}


@router.get("/staff/{row_id}")
async def read_staff(
    row_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    data = await repo.get_staff(pool, row_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "staff_not_found"})
    return data


@router.post("/staff", status_code=status.HTTP_201_CREATED)
async def create_staff(
    payload: StaffCreate,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = admin
    try:
        return await repo.create_staff(
            pool,
            staff_code=payload.staff_code,
            full_name=payload.full_name,
            store_id=payload.store_id,
            role=payload.role,
            brand_id=payload.brand_id,
        )
    except repo.DuplicateCodeError as exc:
        raise _dup(exc, "duplicate_staff_code")
    except repo.NotFoundError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc) or "store_not_found"})


@router.patch("/staff/{row_id}")
async def patch_staff(
    row_id: UUID,
    payload: StaffUpdate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    try:
        data = await repo.update_staff(
            pool,
            row_id,
            full_name=payload.full_name,
            store_id=payload.store_id,
            clear_store=payload.clear_store,
            role=payload.role,
            left_at=payload.left_at,
            left_at_set="left_at" in payload.model_fields_set,
            brand_id=payload.brand_id,
            brand_id_set="brand_id" in payload.model_fields_set,
        )
    except repo.NotFoundError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc) or "store_not_found"})
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "staff_not_found"})
    return data


@router.delete("/staff/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_staff(
    row_id: UUID,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    _ = admin
    try:
        gone = await repo.delete_staff(pool, row_id)
    except repo.LinkedError:
        raise HTTPException(status_code=409, detail={"code": "staff_has_users"})
    if not gone:
        raise HTTPException(status_code=404, detail={"code": "staff_not_found"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- users (account records; disable instead of delete) ---

@router.get("/users")
async def list_users(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    _ = user
    if page is not None:
        return await repo.list_users_offset(
            pool, page=page, page_size=clamp_page_size(page_size), q=q
        )
    try:
        items, next_cursor = await repo.list_users_page(pool, limit=limit, cursor=cursor, q=q)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {"data": items, "next_cursor": next_cursor}


@router.get("/users/unlinked-staff")
async def unlinked_staff(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    """Picker source for the user form: staff with no linked account yet."""
    _ = user
    return {"data": await repo.list_unlinked_staff(pool)}


@router.get("/users/{row_id}")
async def read_user(
    row_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    data = await repo.get_user(pool, row_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found"})
    return data


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = admin
    try:
        return await repo.create_user(
            pool,
            username=payload.username,
            display_name=payload.display_name,
            role=payload.role,
            staff_id=payload.staff_id,
        )
    except repo.DuplicateCodeError as exc:
        raise _dup(exc, "duplicate_username")
    except repo.NotFoundError:
        raise HTTPException(status_code=422, detail={"code": "staff_not_found"})
    except repo.LinkedError:
        raise HTTPException(status_code=409, detail={"code": "staff_already_linked"})


@router.patch("/users/{row_id}")
async def patch_user(
    row_id: UUID,
    payload: UserUpdate,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = admin
    try:
        data = await repo.update_user(
            pool,
            row_id,
            display_name=payload.display_name,
            role=payload.role,
            is_disabled=payload.is_disabled,
            staff_id=payload.staff_id,
            clear_staff=payload.clear_staff,
            staff_set="staff_id" in payload.model_fields_set,
        )
    except repo.NotFoundError:
        raise HTTPException(status_code=422, detail={"code": "staff_not_found"})
    except repo.LinkedError:
        raise HTTPException(status_code=409, detail={"code": "staff_already_linked"})
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found"})
    return data


@router.post("/users/{row_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def set_user_password(
    row_id: UUID,
    payload: PasswordSetIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    token: Annotated[str | None, Depends(get_session_token)] = None,
) -> Response:
    """Admin set/reset of a login password (also enables a never-set account).

    Revokes the target's other sessions, except when the admin resets their
    own password — the caller's session survives (matched by raw token).
    """
    _ = admin
    if len(payload.new) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=422, detail={"code": "weak_password"})
    data = await repo.get_user(pool, row_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found"})
    await auth_repo.set_password_hash(pool, row_id, payload.new)
    # Lock out any sessions the target holds elsewhere; the caller's own
    # session survives (it belongs to the admin, or is their own reset).
    await auth_repo.revoke_other_sessions(
        pool, user_id=row_id, token=token or ""
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _method_not_allowed() -> Response:
    return Response(status_code=405)


for _path in ("/users", "/users/{row_id}"):
    for _method in ("DELETE", "PUT"):
        router.add_api_route(
            _path,
            _method_not_allowed,
            methods=[_method],
            include_in_schema=False,
        )
