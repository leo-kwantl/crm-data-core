"""Connector-framework tests (data D1, 019).

Pure unit: registry validation (validate-on-create) + log/quarantine
redaction (sanitize-on-read). No DB.
"""
from __future__ import annotations

import pytest

from pipelines.ingest import connectors as C


def test_all_source_types_validate_with_required_keys():
    cfgs = {
        "shopify": {"shop_domain": "demo.myshopify.com"},
        "csv": {"column_map": {"member": "member_id"}},
        "excel": {"column_map": {"member": "member_id"}, "sheet": "Orders"},
        "delta_lake": {"table": "lake.orders"},
        "rest_api": {"base_url": "https://api.example.com"},
    }
    for st, cfg in cfgs.items():
        rec = C.validate_connector(st, f"test-{st}", cfg)
        assert rec["source_type"] == st
        assert rec["config"] == cfg


def test_unknown_source_type_rejected():
    with pytest.raises(ValueError, match="unknown source_type"):
        C.validate_connector("salesforce", "x", {})


def test_woocommerce_reserved_rejected():
    with pytest.raises(ValueError, match="reserved"):
        C.validate_connector("woocommerce", "x", {"shop_domain": "y"})


def test_missing_required_key_rejected():
    with pytest.raises(ValueError, match="shop_domain"):
        C.validate_connector("shopify", "x", {})
    with pytest.raises(ValueError, match="column_map"):
        C.validate_connector("csv", "x", {"delimiter": ","})


def test_blank_name_rejected():
    with pytest.raises(ValueError, match="name is required"):
        C.validate_connector("csv", "  ", {"column_map": {}})


def test_embedded_secrets_rejected():
    with pytest.raises(ValueError, match="must not embed secrets"):
        C.validate_connector("shopify", "x", {
            "shop_domain": "d", "api_token": "shpat-SECRET"})
    with pytest.raises(ValueError, match="must not embed secrets"):
        C.validate_connector("rest_api", "x", {
            "base_url": "https://a", "password": "hunter2"})


def test_secret_ref_must_be_env_name():
    with pytest.raises(ValueError, match="env-var name"):
        C.validate_connector("csv", "x", {"column_map": {}},
                             secret_ref="has a space")
    rec = C.validate_connector("shopify", "x", {"shop_domain": "d"},
                               secret_ref="SHOPIFY_TOKEN")
    assert rec["secret_ref"] == "SHOPIFY_TOKEN"


def test_sanitize_for_log_redacts_pii_and_secrets():
    row = {"member_id": "M-000001", "phone": "+852 9123 4567",
           "email": "A@Example.com", "api_token": "shpat-SECRET",
           "lines": [{"sku": "S1", "qty": 2}],
           "nested": {"password": "hunter2", "ok": 1}}
    out = C.sanitize_for_log(row)
    assert out["member_id"] == "M-000001"
    assert out["phone"] == "***" and out["email"] == "***"
    assert out["api_token"] == "***"
    assert out["lines"] == [{"sku": "S1", "qty": 2}]
    assert out["nested"] == {"password": "***", "ok": 1}
    # input untouched (deep copy)
    assert row["phone"] == "+852 9123 4567"


def test_redact_secrets_keeps_pii():
    row = {"phone": "+852 9123 4567", "api_token": "shpat-SECRET"}
    out = C.redact_secrets(row)
    assert out == {"phone": "+852 9123 4567", "api_token": "***"}


def test_schedule_cron_none_is_manual_only():
    assert C.validate_schedule_cron(None) is None
    assert C.validate_schedule_cron("  ") is None
    assert C.validate_schedule_cron("0 2 * * *") == "0 2 * * *"


def test_schedule_cron_rejects_malformed():
    for bad in ("hourly", "0 2 * *", "0 2 * * * *", "99 2 * * *",
                "0 25 * * *", "*/x * * * *"):
        with pytest.raises(ValueError):
            C.validate_schedule_cron(bad)


def test_pagination_cursor_requires_cursor_param():
    with pytest.raises(ValueError, match="cursor_param"):
        C.validate_pagination({"type": "cursor"})
    out = C.validate_pagination({"type": "cursor",
                                 "cursor_param": "after"})
    assert out["response_key"] == "next_cursor"
    assert C.validate_pagination(None) is None


def test_pagination_page_defaults_and_limit_cap():
    out = C.validate_pagination({"type": "page"})
    assert (out["page_param"], out["limit_param"],
            out["default_limit"]) == ("page", "limit", 100)
    with pytest.raises(ValueError):
        C.validate_pagination({"type": "page", "default_limit": 0})
    with pytest.raises(ValueError):
        C.validate_pagination({"type": "nope"})


def test_pagination_validated_at_registration():
    rec = C.validate_connector("rest_api", "p",
                               {"base_url": "https://a",
                                "pagination": {"type": "cursor",
                                               "cursor_param": "after"}})
    assert rec["config"]["pagination"]["response_key"] == "next_cursor"
    with pytest.raises(ValueError, match="cursor_param"):
        C.validate_connector("rest_api", "p",
                             {"base_url": "https://a",
                              "pagination": {"type": "cursor"}})


def test_since_pushdown_server_first():
    assert C.since_pushdown("shopify") == "server"
    assert C.since_pushdown("rest_api") == "server"
    assert C.since_pushdown("csv") == "client"
    assert C.since_pushdown("delta_lake") == "client"


def test_since_filter_fail_open_flags_undateable():
    from datetime import datetime, timezone
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [{"id": 1, "updated_at": "2026-10-05T00:00:00Z"},
            {"id": 2, "updated_at": "2026-09-01T00:00:00Z"},
            {"id": 3},  # missing date: included + flagged
            {"id": 4, "updated_at": "not-a-date"}]  # same
    kept, flagged = C.since_filter(rows, since)
    assert [r["id"] for r in kept] == [1, 3, 4]
    assert sorted(f["index"] for f in flagged) == [2, 3]


def test_should_retry_and_backoff():
    assert C.should_retry(429) and C.should_retry(503)
    assert not C.should_retry(400) and not C.should_retry(200)
    assert not C.should_retry(ValueError("no"))
    assert C.compute_backoff(0) == 1.0
    assert C.compute_backoff(3) == 8.0
    assert C.compute_backoff(10) == 60.0  # capped
    with pytest.raises(ValueError):
        C.compute_backoff(-1)


def test_sync_log_correlates_run_id():
    import json
    line = C.sync_log("run-1", "extracted", entity="orders",
                      rows_seen=50)
    obj = json.loads(line)
    assert obj["sync_run_id"] == "run-1" and obj["event"] == "extracted"
    assert obj["rows_seen"] == "50"


def test_describe_extract_shape_probe():
    out = C.describe_extract([{"a": 1, "b": 2}, {"b": 3, "c": 4}])
    assert out["columns"] == ["a", "b", "c"]
    assert out["sample_rows"] == 2
