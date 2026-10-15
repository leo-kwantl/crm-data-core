"""LD6 exclusion-list tests (data owns governance).

Pure unit: entry validation (bare IDs rejected), matching, and
normalize() raising ExcludedError (empty default = zero behavior change).
"""
from __future__ import annotations

import pytest

from pipelines.exclusions import (
    ExcludedError,
    check_excluded,
    find_exclusion,
    load_exclusions,
)
from pipelines.ingest.events import normalize as normalize_event
from pipelines.ingest.identity import normalize_binding
from pipelines.ingest.orders import normalize as normalize_order

ENTRY = {"kind": "customer", "value": "M-015300",
         "reason": "test account in prod feed",
         "actor": "steward@example.com", "created_at": "2026-10-15"}


def test_bare_id_entry_rejected():
    with pytest.raises(ValueError):
        load_exclusions([{"kind": "customer", "value": "M-015300"}])


def test_unknown_kind_rejected():
    bad = dict(ENTRY, kind="store")
    with pytest.raises(ValueError):
        load_exclusions([bad])


def test_empty_list_is_noop():
    assert load_exclusions([]) == []
    assert load_exclusions(None) == []
    check_excluded([], "customer", "M-015300")  # no raise


def test_find_exclusion_match_and_miss():
    excl = load_exclusions([ENTRY])
    assert find_exclusion(excl, "customer", "M-015300")["reason"] \
        == "test account in prod feed"
    assert find_exclusion(excl, "customer", "M-000001") is None
    assert find_exclusion(excl, "order", "M-015300") is None
    assert find_exclusion(excl, "customer", None) is None


def test_order_normalize_raises_on_excluded_order():
    excl = load_exclusions([dict(ENTRY, kind="order", value="EXT-9")])
    raw = {"store_id": "s", "external_order_id": "EXT-9",
           "lines": [{"qty": 1, "unit_cents": 100}]}
    with pytest.raises(ExcludedError):
        normalize_order(raw, excl)


def test_order_normalize_clean_without_exclusions():
    raw = {"store_id": "s", "external_order_id": "EXT-1",
           "lines": [{"qty": 2, "unit_cents": 150}]}
    out = normalize_order(raw)  # default empty: unchanged behavior
    assert out["total_cents"] == 300


def test_event_normalize_raises_on_excluded_customer():
    excl = load_exclusions([ENTRY])
    raw = {"customer_id": "c", "member_id": "M-015300", "type": "view",
           "source": "web", "source_event_id": "e1"}
    with pytest.raises(ExcludedError):
        normalize_event(raw, excl)


def test_identity_normalize_raises_on_excluded_member():
    excl = load_exclusions([ENTRY])
    raw = {"member_id": "M-015300", "external_member_id": "x",
           "source": "pos"}
    with pytest.raises(ExcludedError):
        normalize_binding(raw, excl)


def test_excluded_error_is_value_error_with_audit():
    excl = load_exclusions([ENTRY])
    try:
        check_excluded(excl, "customer", "M-015300")
    except ExcludedError as exc:
        assert isinstance(exc, ValueError)
        assert exc.entry["actor"] == "steward@example.com"
    else:
        raise AssertionError("should have raised")
