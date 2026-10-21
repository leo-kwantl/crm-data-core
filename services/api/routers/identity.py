"""Identity endpoints: staff roster + login-account records + password admin.

Auth is session-based (routers/auth.py issues tokens; deps.get_current_user
guards). Only GET /staff (the roster list) stays readable without a session,
and only in a reduced projection (review §7.2-4): a session sends
`x-session-token` and gets the full row. Role gates (deps.require_admin, 403
otherwise): user create/update/password-reset and staff create/update/delete
are admin-only — manager/sa get all data but no account administration.
Backend 403 is the real guard; frontend role-hiding is best-effort only.

PATCH /staff/{id} is admin-only because the body can move a staff row's
brand_id (and its store/role), and a session's brand/group is re-derived from
that row on every request (repos/auth.py get_session_user). A non-admin who
could patch their own row could clear brand_id and turn a brand-scoped session
into a group-wide one, which makes deps.py's scope checks pass (finding 7.2-1).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from ..deps import (
    CurrentUser,
    check_ip_rate_limit,
    client_ip,
    get_current_user,
    get_current_user_optional,
    get_pool,
    get_session_token,
    require_admin,
    session_brand,
)
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

# The pre-login roster (login page staff picker) may name who works here and
# nothing else. `staff_code` + `full_name` are what the picker renders; `role`
# is org structure and `store_code`/`brand_code` locate a person, so neither is
# needed to pick them and both are enumeration surface. An allowlist, not a
# denylist, so a new staff column cannot leak by default (review §7.2-4).
_CACHE_RATE_LIMIT = 120
_PUBLIC_STAFF_FIELDS = frozenset({"staff_code", "full_name"})


def _public_staff(row: dict) -> dict:  # type: ignore[type-arg]
    return {k: v for k, v in row.items() if k in _PUBLIC_STAFF_FIELDS}


@router.get("/staff")
async def list_staff(
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    user: Annotated[CurrentUser | None, Depends(get_current_user_optional)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    """Roster list. Anonymous callers get one page, two fields, and no
    `next_cursor`: the cursor is base64 JSON carrying the row id, so handing
    it to an unauthenticated caller would leak exactly what the projection
    withholds. The login picker only needs the first page; the staff admin
    page is authenticated and pages normally."""
    if user is None:
        # No session to key on, so the address is the rate-limit identity.
        check_ip_rate_limit(client_ip(request), route="staff-directory",
                            limit=_CACHE_RATE_LIMIT)
        items, _ = await repo.list_staff_page(pool, limit=limit, cursor=None, q=q)
        return {"data": [_public_staff(r) for r in items], "next_cursor": None}
    if page is not None:
        return await repo.list_staff_offset(
            pool, page=page, page_size=clamp_page_size(page_size), q=q,
            brand_id=session_brand(user),
        )
    try:
        items, next_cursor = await repo.list_staff_page(
            pool, limit=limit, cursor=cursor, q=q, brand_id=session_brand(user)
        )
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
    data = await repo.get_staff(pool, row_id, session_brand(user))
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
    admin: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = admin
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
            pool, page=page, page_size=clamp_page_size(page_size), q=q,
            brand_id=session_brand(user),
        )
    try:
        items, next_cursor = await repo.list_users_page(
            pool, limit=limit, cursor=cursor, q=q, brand_id=session_brand(user)
        )
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
    return {"data": await repo.list_unlinked_staff(pool, session_brand(user))}


@router.get("/users/{row_id}")
async def read_user(
    row_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    data = await repo.get_user(pool, row_id, session_brand(user))
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
