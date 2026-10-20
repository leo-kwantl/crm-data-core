"""Ticket #88: `'pre-provenance'` attributes history and nothing else.

The repair (`scripts/repair_created_by_2026_10_20.py`) back-stamps rows written
before `created_by` described a writer. The literal it writes must never become
an accepted producer: ml's `require_producer` refuses every name that is not a
real one, and if a writer here started emitting `'pre-provenance'` the gate that
caught this defect would become the gate that hides the next one.

These tests read source, not a database: they are the guard on the *rule*, so
they run wherever the suite runs.
"""

from __future__ import annotations

import importlib.util
import pathlib

DATA_ROOT = pathlib.Path(__file__).resolve().parents[3]
REPAIR = DATA_ROOT / "scripts" / "repair_created_by_2026_10_20.py"


def _repair_module():
    spec = importlib.util.spec_from_file_location("repair_created_by", REPAIR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_backstamp_literal_is_history_only():
    """The literal appears in the repair and in this test -- nowhere else.

    A hit in `pipelines/` or `services/api/` means some writer can stamp a row
    with it, which is exactly the escape the ruling closed.
    """
    module = _repair_module()
    literal = module.BACKSTAMP_LITERAL
    assert literal == "pre-provenance"

    hits = []
    for path in list((DATA_ROOT / "pipelines").rglob("*.py")) + list(
            (DATA_ROOT / "services" / "api").rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if literal in text:
            hits.append(str(path.relative_to(DATA_ROOT)))

    assert hits == [], (
        f"{hits} mention {literal!r}; it is a back-stamp for history, never a "
        "producer identity -- a writer emitting it would hide the next "
        "unattributed row instead of failing on it")


def test_repair_only_deletes_named_probe_authors():
    """The delete set is the reviewed list, not a wildcard: no `LIKE`, no NULL."""
    module = _repair_module()
    assert set(module.PROBE_AUTHORS) == {
        "provenance-probe", "cost-probe", "probe", "ml-rehearsal", "crm-ml"}
    assert None not in module.PROBE_AUTHORS
    assert set(module.EXPECTED_PROBE_DELETE) == {
        "clv_snapshots", "churn_snapshots", "assoc_batches", "rec_batches",
        "model_training_runs"}
    assert set(module.EXPECTED_BACKSTAMP) == {
        "churn_snapshots", "rfm_snapshots", "segment_runs", "explain_batches",
        "assoc_batches", "model_training_runs"}
    # The lineage table's scope was extended on 2026-10-20 (ml's ask), so its
    # counts are pinned here as MEASURED numbers, not carried over: a scope
    # that grows takes new counts with it.
    assert module.EXPECTED_PROBE_DELETE["model_training_runs"] == 32
    assert module.EXPECTED_BACKSTAMP["model_training_runs"] == 36


def test_the_two_null_groups_stay_split():
    """Probe-authored rows and NULL history share no table action.

    The ruling's whole point: 11 NULL churn snapshots are churn's history, so a
    set that deletes NULLs in a back-stamp table must be impossible to write by
    accident.
    """
    module = _repair_module()
    joined = set(module.EXPECTED_PROBE_DELETE) & set(module.EXPECTED_BACKSTAMP)
    assert joined == {"churn_snapshots", "assoc_batches", "model_training_runs"}, (
        "only these legitimately carry both: their probe rows go, their "
        f"NULL rows stay. Overlap now: {sorted(joined)}")
