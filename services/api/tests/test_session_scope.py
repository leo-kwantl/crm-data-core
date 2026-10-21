"""A114's read side, in data's harness: the GROUP IS NOT A SCOPE.

App's `15be3e0` moved the session rule (app master `df20bd7`): the scope
comes from the staff row's own shape, not from the role. A staff row that
carries a BRAND reads that brand; a staff row that carries NEITHER brand
nor group reads the global scope; a staff row that carries a GROUP and no
brand has NO analytics scope at all and every analytics read answers 403
`{"code": "no_analytics_scope"}` — never a group scope (which no writer
can produce) and never a zero-filled page that reads as "no activity".

Data's harness used to be able to "prove" the withdrawn scope, because it
held the old copy of the runtime. These checks are the new shape, and the
last one is an ENDPOINT check: the answer is a 403 with that code, not a
group scope and not an empty payload.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.analytics import scope
from services.api.deps import scope_filters


def _staff(brand=None, group=None, role="sa"):
    """A staff row. The role is passed on purpose: it must not matter."""
    return SimpleNamespace(role=role, brand_id=brand, group_id=group)


def test_a_group_without_a_brand_has_no_scope_at_all() -> None:
    s = scope.session_scope(_staff(group="g-1"))
    assert s.kind == "none"
    assert s.has_scope is False
    assert s.brand_id is None
    assert s.group_id is None


def test_the_role_is_not_part_of_the_rule_any_more() -> None:
    """A manager linked to a brand reads that brand, like an `sa` does."""
    for role in ("sa", "manager", "admin"):
        s = scope.session_scope(_staff(brand="b-1", role=role))
        assert s.kind == "brand", role
        assert s.brand_id == "b-1"


def test_a_session_with_no_scope_reads_no_row_at_all() -> None:
    u = _staff(group="g-1", role="manager")
    assert scope.row_visible(u, {"brand_id": None, "group_id": None}) is False
    assert scope.row_visible(u, {"brand_id": "b-1", "group_id": None}) is False
    assert scope.row_visible(u, {"brand_id": None, "group_id": "g-1"}) is False


def test_the_scope_funnel_refuses_a_session_with_no_scope() -> None:
    """`scope_filters` is the funnel every analytics read goes through."""
    with pytest.raises(HTTPException) as exc:
        scope_filters(_staff(group="g-1", role="manager"))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == scope.NO_ANALYTICS_SCOPE
    assert exc.value.detail["reason"] == scope.NO_SCOPE_REASON


def test_a_brand_session_keeps_reading_its_brand() -> None:
    """The shape that must NOT break: a brand scope is (brand, None)."""
    assert scope_filters(_staff(brand="b-1")) == ("b-1", None)
    assert scope_filters(_staff()) == (None, None)


@pytest.mark.asyncio
async def test_an_analytics_read_answers_403_not_a_group_scope() -> None:
    """The endpoint answer, in data's own harness: a session with no scope
    is refused out loud, with the code of its own."""
    from httpx import ASGITransport, AsyncClient

    from services.api.app import create_app
    from services.api.deps import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _staff(
        group="11111111-1111-1111-1111-111111111111", role="manager")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/stats/overview")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == scope.NO_ANALYTICS_SCOPE
    assert detail["reason"] == scope.NO_SCOPE_REASON
