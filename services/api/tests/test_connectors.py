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
