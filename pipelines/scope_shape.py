"""The scope vocabulary a data run may use (A114, 2026-10-22).

The rule is the manager root's `docs/conventions.md` § Scope shape (root
revision `e4a8e4f`): a scope is the PAIR `(brand_id, group_id)`, and a
group scope is only ever the pair -- the writers filter the customer's
group AND the brand's group, so a group ACROSS brands is a shape no writer
can produce. Two shapes are refused today, because no reader can select a
row stamped with either of them:

- (None, group_id): the group alone -- the shape A114 withdrew.
- (brand_id, group_id): the pair -- it is the shape a group cut would
  use, but no session scope can promise it yet (a session reads one of the
  two or neither -- app's `analytics/scope.py`), so a row of that shape
  sits in a scope-shaped table where no reader can select it.

Such a row is not a wrong number, it is an INVISIBLE one: no reader can
select it and no reader can be blamed for it missing. The refusal lives
here, once, so data's CLIs cannot drift apart from ml's eleven job CLIs
(`ml_engine/scope_shape.py`, the same helper and the same refusal text)
-- each CLI calls `require_selectable_scope` at its own boundary, before
it connects and before a flag is converted, so a run that asks for a
shape no reader can select never reaches the cluster.

A group cut relaxes this guard in the same change that gives the portal a
session scope which can promise the pair -- a priced and measured change
(product-decisions A114), never this one.
"""

from __future__ import annotations

__all__ = ["UnselectableScopeRefused", "require_selectable_scope"]


class UnselectableScopeRefused(ValueError):
    """A run asked for a scope no reader can select.

    A `ValueError` because the argument is the problem, so a CLI that
    already prints its refusals names it unchanged.
    """


def require_selectable_scope(brand_id, group_id) -> None:
    """Refuse a scope no reader can select; return None for a legal one.

    Falsy counts as "no scope" (the CLIs' own `_uuid_or_none` reads an
    empty flag as None), so an empty flag is not a scope either.
    """
    if not group_id:
        return
    if not brand_id:
        raise UnselectableScopeRefused(
            "refused the scope (None, group_id): a group scope exists only "
            "as the pair (brand_id, group_id) -- a group across brands is a "
            "scope no writer can produce -- so no reader can select a row "
            "stamped with it (the group scope was withdrawn, "
            "product-decisions A114, 2026-10-22) and the write would be "
            "invisible. Run with --brand-id, or with no scope at all for "
            "the global run.")
    raise UnselectableScopeRefused(
        "refused the scope (brand_id, group_id): the pair is the shape a "
        "group cut would use, but no session scope can promise it yet -- a "
        "session reads one of the two or neither (app's scope.py) -- so no "
        "reader can select a row stamped with it and the write would be "
        "invisible. The guard is relaxed in the same change that gives the "
        "portal a session scope which can promise the pair.")
