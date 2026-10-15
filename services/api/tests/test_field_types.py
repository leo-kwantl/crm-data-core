"""LD10 canonical type-table tests (validation aid only)."""
from __future__ import annotations

import pytest

from pipelines.field_types import FIELD_TYPES, type_mismatches


def test_clean_records_have_no_mismatches():
    assert type_mismatches("orders", {
        "store_id": "S1", "external_order_id": "E1",
        "total_cents": 200}) == []
    assert type_mismatches("events", {
        "type": "view", "payload": {"a": 1}}) == []


def test_mismatches_are_advisory_strings():
    out = type_mismatches("orders", {"total_cents": "200"})
    assert out == ["orders.total_cents: want integer, got str"]


def test_bool_is_not_integer():
    assert type_mismatches("orders", {"total_cents": True}) != []


def test_nulls_skipped_and_unknown_fields_ignored():
    assert type_mismatches("customers", {
        "member_id": None, "whatever_custom": 123}) == []


def test_unknown_entity_rejected():
    with pytest.raises(ValueError):
        type_mismatches("stores", {})


def test_table_covers_all_ingest_entities():
    assert set(FIELD_TYPES) == {
        "customers", "orders", "events", "identity", "products"}
