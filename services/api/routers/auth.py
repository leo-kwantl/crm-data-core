"""Password-login endpoints (public login; session-guarded me/logout/change).

Admin-provisioned accounts only — no self-signup, no SSO. Login accepts a
username OR a linked staff_code as the identifier, but failures are a uniform
401 (code invalid_credentials) so accounts cannot be enumerated. Passwords
never appear in logs; only SHA-256 hex of tokens is stored (repos/auth.py).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import CurrentUser, get_current_user, get_pool, get_session_token
from ..repos import auth as repo
from ..schemas.auth import LoginIn, LoginOut, MeOut, PasswordChangeIn

router = APIRouter(tags=["auth"])

MIN_PASSWORD_LEN = 8


def _bad_credentials() -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "invalid_credentials"})


@router.post("/auth/login", response_model=LoginOut)
async def login(
    payload: LoginIn,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    identity = await repo.find_login_identity(pool, payload.identifier)
    if identity is None or identity.get("is_disabled"):
        raise _bad_credentials()
    if not identity.get("password_hash"):
        raise _bad_credentials()  # admin has not set a password yet
    if not repo.check_password(payload.password, identity["password_hash"]):
        raise _bad_credentials()
    staff_code = repo.session_staff_code(identity)
    token, _ = await repo.create_session(
        pool, user_id=UUID(identity["id"]), staff_code=staff_code
    )
    # Session brand derives from the staff link (no brand picker at login).
    brand = await repo.login_brand_context(pool, UUID(identity["id"]))
    return {
        "token": token,
        "staff_code": staff_code,
        "role": identity["role"],
        "user_id": identity["id"],
        **brand,
    }


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    token: Annotated[str | None, Depends(get_session_token)] = None,
) -> Response:
    _ = user
    if token:
        await repo.revoke_session(pool, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=MeOut)
async def me(user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict:
    return {
        "user_id": str(user.user_id),
        "username": user.username,
        "staff_code": user.staff_code,
        "role": user.role,
        "brand_id": str(user.brand_id) if user.brand_id else None,
        "brand_code": user.brand_code,
        "brand_name": user.brand_name,
        "group_id": str(user.group_id) if user.group_id else None,
        "group_code": user.group_code,
        "group_name": user.group_name,
    }


@router.put("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChangeIn,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    token: Annotated[str | None, Depends(get_session_token)] = None,
) -> Response:
    if len(payload.new) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=422, detail={"code": "weak_password"})
    identity = await repo.find_login_identity(pool, user.username)
    if (
        identity is None
        or not identity.get("password_hash")
        or not repo.check_password(payload.current, identity["password_hash"])
    ):
        raise HTTPException(status_code=401, detail={"code": "invalid_current_password"})
    await repo.set_password_hash(pool, user.user_id, payload.new)
    if token:
        await repo.revoke_other_sessions(pool, user_id=user.user_id, token=token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
