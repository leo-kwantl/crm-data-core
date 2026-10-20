"""The normalizer's payload and the writer's reads are one contract, two ends.

`dict[str, Any]` at the write site hid a class of drift: a key renamed in
`normalize_*` and read under its old name by `apply._apply_*` either raises
`KeyError` (loud, fine) or, where the writer uses `.get`, writes NULL into the
column and looks healthy. TypedDicts at the write sites named the shapes; this
pins them to the code that consumes them, so a rename on either side goes red
and an unconsumed key has to be declared rather than discovered later.

The allowlists are the interesting part: they name keys that are deliberately
not read by their applier, which is what turns "nobody reads this" from a
suspicion into a statement someone signed.
"""
from __future__ import annotations

import inspect
import re
import typing

import pytest

from pipelines import apply as ap
from pipelines.ingest.events import EventPayload, normalize as normalize_event
from pipelines.ingest.identity import (IdentityPayload,
                                       normalize_binding)
from pipelines.ingest.orders import OrderPayload, normalize as normalize_order
from pipelines.upload import CustomerPayload, normalize_customer

# key -> why the applier never reads it (each entry is a claim, not a waiver)
UNCONSUMED = {
    "customers": {
        "phone": "the contact writer reads the RAW row, not the payload",
        "email": "the contact writer reads the RAW row, not the payload",
    },
    "orders": {
        "customer_id": "the applier resolves from the RAW row (3 candidate refs)",
    },
    "events": {},
    "identity": {},
}

CASES = [
    ("customers", CustomerPayload, ap._apply_customer),
    ("orders", OrderPayload, ap._apply_order),
    ("events", EventPayload, ap._apply_event),
    ("identity", IdentityPayload, ap._apply_identity),
]

_READ = re.compile(r"norm(?:\.get\(|\[)\s*\"([a-z_]+)\"")


def _read_keys(fn) -> set[str]:
    """Every `norm[...]` / `norm.get(...)` key in an applier's own source."""
    return set(_READ.findall(inspect.getsource(fn)))


@pytest.mark.parametrize("kind,shape,applier", CASES,
                         ids=[c[0] for c in CASES])
def test_every_declared_key_is_read_or_declared_unconsumed(kind, shape, applier):
    declared = set(shape.__annotations__)
    unread = declared - _read_keys(applier) - set(UNCONSUMED[kind])
    assert not unread, (
        f"{kind}: {sorted(unread)} is declared on the payload but no writer "
        "reads it -- delete it, or add it to UNCONSUMED with the reason")


@pytest.mark.parametrize("kind,shape,applier", CASES,
                         ids=[c[0] for c in CASES])
def test_the_writer_reads_no_undeclared_key(kind, shape, applier):
    declared = set(shape.__annotations__)
    undeclared = _read_keys(applier) - declared
    assert not undeclared, (
        f"{kind}: the writer reads {sorted(undeclared)}, which the payload "
        "shape does not declare -- a rename that TypeDict would not catch")


def test_the_allowlist_does_not_outlive_the_read():
    """A key coming back into use must leave UNCONSUMED, or the list rots."""
    stale = {kind: sorted(set(keys) & _read_keys(applier))
             for kind, shape, applier in CASES
             for keys in [UNCONSUMED[kind]]}
    assert not {k: v for k, v in stale.items() if v}, (
        f"read again but still allowlisted: {stale}")


# the normalizers must actually produce every required key, not just declare it
SAMPLES = {
    "customers": (CustomerPayload, normalize_customer,
                  {"member_id": "M-000123", "full_name": "Test Person"}),
    "orders": (OrderPayload, normalize_order,
               {"store_id": "CWB-01", "external_order_id": "EXT-1",
                "lines": [{"sku": "SKU-1", "qty": 2, "unit_cents": 1250}]}),
    "events": (EventPayload, normalize_event,
               {"customer_id": "00000000-0000-0000-0000-000000000000",
                "type": "purchase", "source": "pos", "source_event_id": "E-1"}),
    "identity": (IdentityPayload, normalize_binding,
                 {"member_id": "M-000123", "external_member_id": "EXT-9",
                  "source": "pos"}),
}


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_the_normalizer_sets_every_required_key(kind):
    shape, normalize, raw = SAMPLES[kind]
    out = normalize(dict(raw))
    # `from __future__ import annotations` leaves ForwardRefs behind, so the
    # optional marker is resolved rather than read off the raw string
    required = {k for k, v in typing.get_type_hints(shape,
                                                   include_extras=True).items()
                if typing.get_origin(v) is not typing.NotRequired}
    missing = required - set(out)
    assert not missing, f"{kind}: normalizer produced no {sorted(missing)}"
    extra = set(out) - set(shape.__annotations__)
    assert not extra, f"{kind}: normalizer emitted undeclared {sorted(extra)}"


@pytest.mark.asyncio
async def test_a_payload_source_that_differs_from_the_upload_is_refused():
    """The upload's source wins; a different claim fails instead of vanishing.

    The row would otherwise be stored under `apply`'s source while the payload
    said `web` -- wrong provenance, nothing raised, no test able to see it.
    """
    from pipelines.ingest.errors import RowRejected
    norm = normalize_event(dict(SAMPLES["events"][2]))
    with pytest.raises(RowRejected) as exc:
        await ap._apply_event(None, norm, {}, "a-different-source")
    assert "a-different-source" in str(exc.value)


def test_order_lines_use_the_declared_line_shape():
    from pipelines.ingest.orders import OrderLine
    lines = normalize_order(SAMPLES["orders"][2])["lines"]
    assert set(lines[0]) == set(OrderLine.__annotations__)
