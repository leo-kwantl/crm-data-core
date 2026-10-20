"""The catalogue's category vocabulary is a model input, not decoration.

`ml_engine/segmentation.py:404-409` builds a customer's feature vector as a
one-hot over the *sorted `products.category` values it is handed*, so the width
of every segment fit is decided by this table, not by the transactions: one
order-less product with a novel category widens the space of every later run
(measured 2026-10-20: a fixture row with `category='test'` moved the demo brand
from 5 to 6 wide, `pipelines/contracts.md` § 015).

Two guards, both cheap and both aimed at the same failure: a category that is
not part of the seeded catalogue entering the vocabulary unnoticed.

Resolved 2026-10-20, in favour of the guard: the sixth category measured live
was `test`, carried by `APPLY-SKU` -- a row `test_apply.py::_seed` inserted,
not demo data. That fixture seeds `frames` now and purges its own namespace on
teardown, so the expected set here stays: a rehearsal is not a reason to widen
a vocabulary every later segment fit reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SEED = REPO / "scripts" / "seed_catalog.py"

# The five categories the demo catalogue is built from (`NAMES` in the seed).
EXPECTED_CATEGORIES = {"accessories", "care", "frames", "lenses", "sunglasses"}

# A category a rehearsal wrote, not a business one -- the shape that widened a
# live geometry on 2026-10-20 before it was deleted.
REHEARSAL_MARKERS = ("test", "probe", "rehearsal", "tmp", "dummy")


def _seeded_categories() -> set[str]:
    """The keys of `NAMES` in the seed script, read as source (no import)."""
    match = re.search(r"^NAMES = \{(.*?)\}", open(SEED, encoding="utf-8").read(),
                      re.S | re.M)
    assert match, "seed_catalog.py no longer defines NAMES at module level"
    return set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', match.group(1)))


def test_the_seed_builds_exactly_the_five_business_categories() -> None:
    assert _seeded_categories() == EXPECTED_CATEGORIES, (
        "the seeded vocabulary changed: a new category widens every segment "
        "fit's feature space (contracts.md § 015), so change this set only "
        "alongside a deliberate model-side decision")


def test_no_seeded_category_smells_like_a_rehearsal() -> None:
    offenders = sorted(
        c for c in _seeded_categories()
        if any(marker in c.lower() for marker in REHEARSAL_MARKERS))
    assert offenders == [], (
        f"rehearsal-shaped categories in the seed: {offenders} -- they become "
        "model dimensions for every customer, not just the fixture's")


@pytest.mark.asyncio
async def test_live_catalogue_carries_no_rehearsal_category(
        database_url: str) -> None:
    """A seeded DB must not serve a category outside the business set.

    Skips on an unseeded cluster (no products at all) rather than passing
    vacuously: the guard is about what a *seeded* catalogue contains.
    """
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT category FROM products ORDER BY 1")
            live = {r["category"] for r in rows if r["category"]}
    finally:
        await pool.close()

    if not live:
        pytest.skip("catalogue empty on this cluster: nothing to read")

    extra = sorted(live - EXPECTED_CATEGORIES)
    assert extra == [], (
        f"categories outside the seeded catalogue: {extra} -- each one is a "
        "feature dimension in the next segment fit (contracts.md § 015)")
