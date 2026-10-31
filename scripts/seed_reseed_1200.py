"""Reseed generator: 1,200 realistic customers (DEV-ONLY, INSERT-only).

Sibling to seed_dev_history.py, built for the owner-directed reseed
(runbook: pushes -> seed_catalog.py -> wipe -> this seed ->
run_pipeline nightly -> scheduler cohort snapshot engine -> ml flows
-> serving verify). `--wipe-analytics` clears all 30 regenerable
snapshot/batch tables (see ANALYTICS_TABLES; guarded by
services/api/tests/test_seed_wipe.py) and deliberately keeps
NEVER_WIPE ones (watermarks, connector config, report definitions,
sessions). Re-running over an EXISTING seed inserts nothing (members
and orders are skipped), so pass `--reset-seed-block` to delete this
seeder's own rows (`M-03%` members, `RS3-%` orders) and rebuild the
dataset -- required for a distribution change to take effect. Note
`run_daily_snapshot.py` takes no --database-url: it reads the
DATABASE_URL env var. Ordering that matters: run_pipeline.py and the cohort
snapshot engine must run BEFORE the ML slots, because the segments
step reads RFM scores and the serving verify reads cohort snapshots
(after a wipe, /cohort is empty until that engine runs; a rebuilt
dataset reads 940 cohort members). INSERT-only: bootstrap identity rows
(users/staff/tiers/campaigns/brands/groups) are never touched, only
referenced. Rerun-safe: existing member_ids and external order ids
are skipped in Python (deterministic RNG stream, seed 7).

DETERMINISM IS CONDITIONAL ON DB STATE AND ON THIS REVISION, not just
on the seed: baskets draw from the `products` table, home stores from
`stores`/`brands` and the campaign slice from `campaigns`, so the same
seed over a different catalog yields different baskets and revenue. The
COHORT LADDER is the loudest difference between revisions, which is why
the summary line prints it: `SIGNUP_MONTHS = 36` (commit 510f62c and
later) gives 36 first-order months starting 2023-11-12 with 8,670
M-03 orders, while the pre-510f62c 24-month ladder gives 25
first-order months starting 2024-10-25 with 9,357. Those are block-only
counts; TOTALS add the 8 bootstrap demo customers / ~15 orders, i.e.
8,685 vs 9,372 -- so a run reporting `orders=9372` or a 2024-10 first
order ran a STALE CHECKOUT of this script, not a different catalog (a
catalog swap moves baskets/revenue, never the ladder). Reference binds
on this revision (totals): customers 1,280 / orders 10,755 /
top20_share 0.74 / max_recency_d 409 (measured 2026-10-31 against
CURRENT_DATE, so it grows by 1 per day) / full_optout 108. The
+12 customers / +228 orders over the 1,208 / 8,685 base are the
2026-10-31 recent fill (12 M-031201.. signups, RS3-R-* orders
2026-10-16..2026-10-30); the +60 / +1,842 over that are the
restored M-02 dev-history block (seed_dev_history.py rerun).

Distribution spec (owner-approved, bound 2026-10-15):
- 1,200 customers, M-03 block (M-030001..M-031200; no collision with
  M-0001xx demo / M-020xxx dev-history).
- Age Normal(38,12) clipped 18..80 -> date_of_birth; signup spread
  36 months (3y ladder for /cohort) on a 3x compounding growth curve
  + weekly seasonality.
- Cohort realism (020 cohort = month of a member's FIRST completed
  order): the first order lands within ~6 weeks of signup and the last
  order is pinned at the drawn recency, so `cohort_snapshots` ladders
  every month of the signup window instead of piling into the last
  year. Needs >= 2 orders whenever recency > 45d after signup.
- Order counts Pareto-lognormal mix: top ~20% of customers yield
  ~80% of revenue (VIP lognormal(mu=3.3) vs base lognormal(mu=0.5),
  tuned; the measured share is printed at the end of every run).
  Counts are CLIPPED at 60, which leaves a known boundary pile-up (~26
  customers sit exactly at 60 while 1-58 decays smoothly). A soft tail
  (squash instead of min) removes the artifact, but it moves the
  revenue/tail binds, so it is a deliberate follow-up rather than a
  silent retune.
- Orders spread over every brand's stores: each customer gets a
  deterministic home store (85% repeat visits), so brand-scoped
  /cohort, /rfm and /segments views are populated for all brands.
- Zero-order members are kept (base lognormal truncation, ~19%): real
  no-purchase members, and by contract they never appear in a cohort,
  so cohort members are always < the customer count.
- Baskets: 1-3 lines from real catalog products (category-weighted:
  care/accessories frequent, frames rare), qty 1-3, total = lines sum
  (lognormal-ish around catalog tiers).
- Recency: last-order mixture 45% 0-60d / 30% 60-180d / 25% 180-400d
  so at-risk/lost segments populate; the first order sits near signup
  and later ones spread in between with Dec-peak/Feb-trough seasonal
  WEIGHTS (12=1.8, 11=1.5, 10=1.2, 2=0.7). Observed monthly order
  COUNTS peak in Aug/Sep, not Dec: the recency mixture concentrates
  mass near ANCHOR and Dec 2026 is beyond it, so only Dec 2025 carries
  the December weight (Feb is the trough either way).
- contacts + consents for every customer; ~8% full opt-out.
- Store visits span every brand (home store per customer, 85% repeat
  visits) so brand-scoped RFM / cohort / segment pages have data.
- Tiers assigned by spend decile (top 10% PLATINUM, next 25% GOLD).
- Catalog campaign membership: every campaign seeded by
  `seed_catalog.py` gets a deterministic 120-300 member slice -- a
  blake2b-ranked cut of the M-03% pool, ranked independently per
  campaign so the slices overlap a little (real customers receive two
  campaigns). Additive only: demo/bootstrap assignments are never
  touched, and a `--reset-seed-block` re-run reproduces the same
  membership exactly.
- Acquisition-channel fill (2026-10-31, additive): every M-03% row
  still 'unknown' gets one draw from its per-member stream over the
  060 vocab (walk_in 35 / online 25 / referral 15 / social 12 /
  phone 8 / event 5); demo and dev-history rows are never in the
  predicate, and a rerun updates nothing.
- Recent fill 2026-10-16..2026-10-30 (2026-10-31, additive): ~15% of
  M-03 members get 1-2 extra completed orders dated in-window
  (`RS3-R-*`, same source-tag family, skip-if-exists) with baskets
  from the live catalog, plus 12 new M-031201.. signups created
  in-window with one first order each. Per-member streams, so a
  rerun inserts nothing.

Run (after the bootstrap wipe and AFTER `seed_catalog.py` -- the
campaign-member slice below needs the catalog campaigns to exist,
otherwise those campaigns quietly keep only the handful of members
seed_catalog assigned; before the pipeline):
    python scripts/seed_reseed_1200.py --database-url postgresql://...
    python scripts/seed_reseed_1200.py --database-url ... --wipe-analytics

--wipe-analytics executes the analytics TRUNCATE list (opt-in, default
off) over every regenerable job output: D2 snapshots (churn/clv/kpi/
cohort), RFM snapshots + segment size/movement, recs, segments (+
explain / matrix / surrogate rows via CASCADE), assoc, forecast,
training runs, btyd params, landing receipts, transient upload staging.
Stale rows here are the classic "torn ladder" bug: a reseed that leaves
cohort_snapshots or kpi_daily_snapshots in place mixes two datasets on
the app's pages, so the list must stay complete whenever a migration
adds a snapshot table (services/api/tests/test_seed_wipe.py enforces
that: a hint-matching new table must join ANALYTICS_TABLES or the
explicit NEVER_WIPE list). pipeline_manifests (watermark),
schema_migrations, users, staff, tiers, campaigns, brands, groups,
stores, products are NEVER wiped.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg

SEED = 7
N_MEMBERS = 1200
MEMBER_SEQ_BASE = 30000  # M-030001..M-031200
SOURCE_TAG = "RS3"
ANCHOR = date(2026, 10, 15)  # reseed as-of; deterministic dates
SIGNUP_MONTHS = 36  # 3-year cohort ladder ending ANCHOR
SIGNUP_GROWTH = 3.0  # newest signup month ~3x the oldest

SEASONALITY = {1: 0.9, 2: 0.7, 3: 1.0, 4: 1.0, 5: 1.1, 6: 1.0,
               7: 1.0, 8: 1.1, 9: 1.0, 10: 1.2, 11: 1.5, 12: 1.8}
# Signup growth: compounding weight 1.0 -> 3.0 over the 36mo window.
WEEKDAY_W = [0.9, 0.9, 1.0, 1.0, 1.1, 1.4, 1.3]  # Mon..Sun

# Store choice spans EVERY store in the DB (brand-scoped pages need
# orders per brand, not just the demo doors). Weight = brand share /
# that brand's store count, so a brand does not dominate by door count.
BRAND_SHARE = {"DEMO": 0.34, "SUNHING": 0.20, "KVLN": 0.16,
               "VICEW": 0.14, "HARBOR": 0.10, "PEAKLENS": 0.06}
HOME_STORE_P = 0.85  # else a cross-brand visit
# (category, pick weight): cheap care/accessories frequent, frames rare.
CAT_W = {"care": 3.0, "accessories": 3.0, "lenses": 2.0,
         "sunglasses": 1.2, "frames": 1.0}
CHANNELS = ("sms", "email", "whatsapp")
# Acquisition-channel split over the 060 vocab (owner-approved
# 2026-10-31): cumulative-draw shares, percentages of the M-03% block.
CHANNEL_SPLIT = (
    ("walk_in", 35.0),
    ("online", 25.0),
    ("referral", 15.0),
    ("social", 12.0),
    ("phone", 8.0),
    ("event", 5.0),
)
# Recent fill window (additive, 2026-10-31): a deterministic subset of
# M-03 members gets 1-2 extra completed orders dated in-window, plus a
# handful of new M-03 signups created in-window.
RECENT_START = date(2026, 10, 16)
RECENT_END = date(2026, 10, 30)
RECENT_P = 0.15  # share of M-03 members getting extra recent orders
RECENT_SECOND_P = 0.30  # share of those getting a 2nd recent order
RECENT_NEW_SIGNUPS = 12  # new members M-031201..M-031212, created in-window
RECENT_SEQ_BASE = 31201
RECENT_SOURCE_TAG = f"{SOURCE_TAG}-R"  # RS3-R-*: same family, reset-covered
DISTRICTS = ["Central", "Causeway Bay", "Tsim Sha Tsui", "Mong Kok",
             "Sha Tin", "Tuen Mun", "Kwun Tong", "Wan Chai"]
SURNAMES = ["Chan", "Wong", "Lee", "Cheung", "Lau", "Ng", "Chow", "Chu",
            "Ho", "Yeung", "Tsang", "Tang", "Cheng", "Lam", "Leung"]
GIVEN = ["Tai Man", "Mei Ling", "Kwok Wai", "Siu Ming", "Ka Yan",
         "Chi Keung", "Wai Lun", "Hoi Yan", "Chun Kit", "Wing Sze",
         "Ka Ho", "Yuk Ching", "Man Ho", "Lai Fong", "Tsz Kin"]
WESTERN = ["Sarah Chen", "David Lau", "Emily Ng", "Peter Ho",
           "Grace Yip", "Kevin So", "Anna Kwok", "Brian Tse"]

ANALYTICS_TABLES = [
    # D2 snapshots (013/020) + the app's /cohort and KPI readers.
    "churn_snapshots", "churn_customer_scores",
    "clv_snapshots", "clv_customer_scores",
    "kpi_daily_snapshots", "model_training_runs",
    "cohort_snapshots",
    # RFM (010/031) + segmentation/recs (011/015).
    "rfm_snapshots", "rfm_customer_scores",
    "rfm_segment_sizes", "rfm_segment_movement",
    "rec_batches", "rec_items",
    "segment_runs", "segment_assignments", "pipeline_runs",
    # ML batch outputs (018/021/022/028/029/030/032/027).
    "assoc_batches", "assoc_rules",
    "explain_batches", "explain_rows",
    "forecast_batches", "forecast_rows",
    "cluster_feature_matrix", "cluster_feature_matrix_long",
    "seg_surrogate_fits",
    "btyd_params", "landing_receipts",
    # Transient upload staging (dev reruns): jobs + their staged rows.
    "upload_jobs", "staging_rows", "quarantine_rows",
]

# Deliberately NOT wiped although they are job output: pipeline
# watermarks, connector config + sync state, report definitions, auth
# sessions. Reference / dataset tables (brands, stores, products,
# campaigns, customers, orders, contacts, consents, custom-data values)
# are owned by bootstrap_db.py (TRUNCATE ... CASCADE).
NEVER_WIPE = [
    "pipeline_manifests", "schema_migrations", "connectors",
    "connector_sync_runs", "report_definitions", "sessions",
]


def _draw_recency_days(rng: random.Random) -> int:
    u = rng.random()
    if u < 0.45:
        return rng.randint(0, 60)
    if u < 0.75:
        return rng.randint(61, 180)
    return rng.randint(181, 400)


def _draw_order_date(rng: random.Random, lo: date, hi: date) -> date:
    """Uniform day in [lo, hi] accepted under seasonal x weekday weight."""
    span = max((hi - lo).days, 0)
    for _ in range(60):
        d = lo + timedelta(days=rng.randint(0, span))
        w = SEASONALITY[d.month] * WEEKDAY_W[d.weekday()]
        if rng.random() < w / (1.8 * 1.4):
            return d
    return lo + timedelta(days=rng.randint(0, span))


def _draw_signup(rng: random.Random) -> date:
    """36mo spread ending ANCHOR: 3x growth curve + weekly seasonality."""
    span = SIGNUP_MONTHS * 30
    for _ in range(60):
        back = rng.randint(0, span)
        d = ANCHOR - timedelta(days=back)
        if d > ANCHOR:
            continue
        growth = SIGNUP_GROWTH ** (1 - back / span)
        accept = growth * WEEKDAY_W[d.weekday()] / (SIGNUP_GROWTH * 1.4)
        if rng.random() < accept:
            return d
    return ANCHOR - timedelta(days=rng.randint(0, span))


async def _reset_seed_block(conn) -> None:
    """Delete every row this seeder owns: `M-03%` members, `RS3-%` orders.

    Needed because the seeder is INSERT-only and skips existing members:
    re-running it over its own output inserts nothing, so a distribution
    change (e.g. the wider signup ladder) could never reach an existing
    dataset. Callers wipe the analytics tables first and that ordering is
    load-bearing: rfm/clv/churn `*_customer_scores` and
    `segment_assignments` reference `customers (id)` with no ON DELETE and
    are only cleared by the wipe (main() makes `--reset-seed-block` imply
    `--wipe-analytics` for exactly this reason).

    Scope is strictly this seeder's own rows (`M-03%` members and the
    orders they own); bootstrap's demo block (`M-00%`) and any other
    legacy customers are deliberately left alone -- their first orders sit
    inside the signup ladder window, and deleting them would change the
    customer count. `customer_access_logs` is unlinked, not deleted: the
    audit trail survives the reseed.
    """
    block = "SELECT id FROM customers WHERE member_id LIKE 'M-03%'"
    stray = await conn.fetchval(
        "SELECT count(*) FROM orders o JOIN customers c"
        " ON c.id = o.customer_id WHERE c.member_id LIKE 'M-03%'"
        " AND o.external_order_id NOT LIKE $1", f"{SOURCE_TAG}-%")
    print(f"reset: preflight: {stray} 'M-03%' order(s) without the"
          f" {SOURCE_TAG}- tag (removed by customer_id regardless)")
    steps: list[tuple[str, str, list[str]]] = [
        ("order_items",
         "DELETE FROM order_items oi USING orders o"
         " WHERE oi.order_id = o.id AND (o.external_order_id LIKE $1"
         f" OR o.customer_id IN ({block}))", [f"{SOURCE_TAG}-%"]),
        ("orders",
         "DELETE FROM orders WHERE external_order_id LIKE $1"
         f" OR customer_id IN ({block})", [f"{SOURCE_TAG}-%"]),
        ("consent_events",
         f"DELETE FROM consent_events WHERE customer_id IN ({block})", []),
        ("consents",
         f"DELETE FROM consents WHERE customer_id IN ({block})", []),
        ("customer_contacts",
         f"DELETE FROM customer_contacts WHERE customer_id IN ({block})",
         []),
        # Audit trail: unlink instead of deleting the log rows.
        ("customer_access_logs (unlink)",
         "UPDATE customer_access_logs SET customer_id = NULL"
         f" WHERE customer_id IN ({block})", []),
        # campaign_members / customer_events / recommendations / custom
        # data are ON DELETE CASCADE; analytics tables are already empty.
        ("customers",
         "DELETE FROM customers WHERE member_id LIKE 'M-03%'", []),
    ]
    for label, sql, params in steps:
        print(f"reset: {label}: {await conn.execute(sql, *params)}")


CATALOG_CAMPAIGN_CODES = (
    "SUMMER-FRAME-FIESTA", "DOUBLE-11-LENS-UPGRADE",
    "MTR-COMMUTER-PRESCRIPTION", "BACK-TO-SCHOOL-KIDS-EYEWEAR",
    "GOLDEN-WEEK-SUNGLASS-EDIT", "VIP-EYE-EXAM-FRAME-BUNDLE",
)
CAMPAIGN_SLICE_MIN = 120  # app-decided band per catalog campaign
CAMPAIGN_SLICE_MAX = 300


def _campaign_slice_size(code: str) -> int:
    """Deterministic member-count target for one campaign (120..300)."""
    h = hashlib.blake2b(code.encode(), digest_size=8).digest()
    span = CAMPAIGN_SLICE_MAX - CAMPAIGN_SLICE_MIN + 1
    return CAMPAIGN_SLICE_MIN + int.from_bytes(h, "big") % span


def _campaign_slice_key(code: str, member_id: str) -> bytes:
    """Rank key for (campaign, member): independent cuts therefore
    overlap (mean ~1 campaign per member at 6x200 over 1,200)."""
    return hashlib.blake2b(f"{code}|{member_id}".encode(),
                           digest_size=8).digest()


def _member_rng(seed: int, salt: int, member_id: str) -> random.Random:
    """Per-member deterministic stream: `seed` + `salt` + member seq.

    Independent of every other member, so a rerun (or a partial prior
    run) draws the same values for a member however many rows already
    exist -- the fixed-stream ethos without positional coupling. int
    seeds only: str seeds hash per-process and are NOT stable."""
    return random.Random(seed * 1_000_003 + salt + int(member_id[2:]))


def _draw_channel(rng: random.Random) -> str:
    """One channel code from CHANNEL_SPLIT (cumulative shares)."""
    u = rng.random() * 100.0
    cum = 0.0
    for code, pct in CHANNEL_SPLIT:
        cum += pct
        if u < cum:
            return code
    return CHANNEL_SPLIT[-1][0]


def _basket_lines(rng: random.Random, pmap: dict, skus: list,
                  weights: list) -> tuple[list, int]:
    """1-3 category-weighted lines from real products; total = sum."""
    lines, total = [], 0
    nlines = (1 + (1 if rng.random() < 0.45 else 0)
              + (1 if rng.random() < 0.15 else 0))
    for _ in range(nlines):
        sku = rng.choices(skus, weights=weights)[0]
        qty = (1 + (1 if rng.random() < 0.25 else 0)
               + (1 if rng.random() < 0.05 else 0))
        price = pmap[sku]["price_cents"]
        total += price * qty
        lines.append((pmap[sku]["id"], qty, price))
    return lines, total


async def _assign_channels(conn, seed: int) -> tuple[int, int]:
    """Deterministic channel fill for the M-03% block (additive).

    Every M-03% row still 'unknown' gets one draw from its per-member
    stream (salt 101), so the block converges on CHANNEL_SPLIT and a
    rerun draws the same code per member but updates nothing (the
    UPDATE is predicated on 'unknown'). Demo (M-0001xx) and
    dev-history (M-02%) rows are never in the predicate."""
    rows = await conn.fetch(
        "SELECT member_id FROM customers"
        " WHERE member_id LIKE 'M-03%'"
        " AND acquisition_channel = 'unknown' ORDER BY member_id")
    updates = [(_draw_channel(_member_rng(seed, 101, r["member_id"])),
                r["member_id"]) for r in rows]
    if updates:
        await conn.executemany(
            "UPDATE customers SET acquisition_channel = $1"
            " WHERE member_id = $2 AND acquisition_channel = 'unknown'",
            updates)
    n = await conn.fetchval(
        "SELECT count(*) FROM customers WHERE member_id LIKE 'M-03%'")
    return n, len(updates)


async def _recent_fill(conn, seed: int, store_ids: list, store_w: list,
                   pmap: dict, skus: list, weights: list,
                   grp) -> dict:
    """Recent fill 2026-10-16..2026-10-30 (additive, rerun = no-op).

    (1) A deterministic subset (RECENT_P) of the PRE-EXISTING M-03
    members gets 1-2 extra completed orders dated in-window
    (external ids RS3-R-<seq>-<k>: the RS3- family, so
    --reset-seed-block still owns them; skip-if-exists in Python).
    Baskets draw from the live products table exactly like the main
    phase (category weights, qty 1-3, total = lines sum); the store is
    the member's most-frequent store at 85% repeat, else a weighted
    cross-brand draw. (2) RECENT_NEW_SIGNUPS new M-03 members
    (M-031201.., created in-window) with contacts + consents and one
    first completed order each near signup. Every draw comes from the
    member's own stream (salt 202/303), so reruns and partial runs
    reproduce each member independently."""
    idmap = {r["member_id"]: str(r["id"]) for r in await conn.fetch(
        "SELECT id, member_id FROM customers"
        " WHERE member_id LIKE 'M-03%'")}
    new_ids = {f"M-{RECENT_SEQ_BASE + i:06d}"
               for i in range(RECENT_NEW_SIGNUPS)}
    have_ext = {r["external_order_id"] for r in await conn.fetch(
        "SELECT external_order_id FROM orders"
        " WHERE external_order_id LIKE $1", f"{RECENT_SOURCE_TAG}-%")}
    home_of = {}
    for r in await conn.fetch(
            "SELECT c.member_id, (SELECT o.store_id FROM orders o"
            " WHERE o.customer_id = c.id GROUP BY o.store_id"
            " ORDER BY count(*) DESC LIMIT 1) AS store_id"
            " FROM customers c WHERE c.member_id LIKE 'M-03%'"):
        home_of[r["member_id"]] = r["store_id"]
    payback, item_rows = [], []
    members_hit = 0
    for m in sorted(idmap):
        if m in new_ids:
            continue  # new signups get their own first order below
        mrng = _member_rng(seed, 202, m)
        if mrng.random() >= RECENT_P:
            continue
        members_hit += 1
        n = 2 if mrng.random() < RECENT_SECOND_P else 1
        home = home_of[m] or mrng.choices(store_ids,
                                          weights=store_w)[0]
        seqn = int(m[2:])
        for k in range(n):
            ext = f"{RECENT_SOURCE_TAG}-{seqn}-{k + 1:02d}"
            if ext in have_ext:
                continue
            od = _draw_order_date(mrng, RECENT_START, RECENT_END)
            ordered = datetime(od.year, od.month, od.day,
                               mrng.randint(9, 20), mrng.randint(0, 59),
                               tzinfo=timezone.utc)
            store = (home if mrng.random() < HOME_STORE_P
                     else mrng.choices(store_ids, weights=store_w)[0])
            lines, total = _basket_lines(mrng, pmap, skus, weights)
            payback.append((idmap[m], store, ordered, total, ext))
            item_rows.extend([(ext, pid, qty, price)
                              for (pid, qty, price) in lines])
    # --- new in-window signups (skip-if-exists in Python) ---
    used_phones = {r["value_normalized"] for r in await conn.fetch(
        "SELECT value_normalized FROM customer_contacts"
        " WHERE kind = 'phone'")}
    cust_rows, contact_rows, consent_rows = [], [], []
    signup_payback, signup_items = [], []
    signups_added = 0
    for m in sorted(new_ids):
        if m in idmap:
            continue
        mrng = _member_rng(seed, 303, m)
        seqn = int(m[2:])
        signup = RECENT_START + timedelta(days=mrng.randint(0, 14))
        age = min(max(int(mrng.gauss(38, 12)), 18), 80)
        dob = signup - timedelta(days=int(age * 365.25))
        if mrng.random() < 0.12:
            name = mrng.choice(WESTERN)
            locale = "en"
        else:
            name = f"{mrng.choice(SURNAMES)} {mrng.choice(GIVEN)}"
            locale = "zh-HK"
        created = datetime(signup.year, signup.month, signup.day,
                           10, 0, tzinfo=timezone.utc)
        cust_rows.append((m, name, created, dob,
                         mrng.choice(["male", "female"]),
                         mrng.choice(DISTRICTS), locale, grp))
        nph = 90000000 + (seqn * 7919) % 9999999
        phone = f"+852{nph:08d}"
        while phone in used_phones:
            nph += 1
            phone = f"+852{nph:08d}"
        used_phones.add(phone)
        contact_rows.append((m, "phone", phone,
                            f"+852 {phone[4:8]} {phone[8:]}", True))
        local = name.lower().replace(" ", ".")
        email = f"{local}.{seqn}@example.com"
        contact_rows.append((m, "email", email, email, False))
        optout = mrng.random() < 0.08
        for ch in CHANNELS:
            if optout:
                st = "opted_out"
            elif ch == "sms":
                st = ("opted_in" if mrng.random() < 0.90
                      else "opted_out")
            elif ch == "email":
                st = ("opted_in" if mrng.random() < 0.85
                      else "opted_out")
            else:
                st = ("opted_in" if mrng.random() < 0.40
                      else "opted_out")
            consent_rows.append((m, ch, st))
        signups_added += 1
    if cust_rows:
        await conn.executemany(
            "INSERT INTO customers (member_id, full_name, created_at,"
            " date_of_birth, gender, district, attributes, group_id)"
            " VALUES ($1,$2,$3,$4,$5,$6,"
            " jsonb_build_object('locale', $7::text), $8::uuid)",
            cust_rows)
        nidmap = {r["member_id"]: str(r["id"]) for r in await conn.fetch(
            "SELECT id, member_id FROM customers"
            " WHERE member_id = ANY($1)",
            [c[0] for c in cust_rows])}
        idmap.update(nidmap)
        await conn.executemany(
            "INSERT INTO customer_contacts (customer_id, kind,"
            " value_normalized, value_display, is_primary)"
            " SELECT c.id, $2, $3, $4, $5 FROM customers c"
            " WHERE c.member_id = $1",
            [(m, kind, norm, disp, prim)
             for (m, kind, norm, disp, prim) in contact_rows])
        await conn.executemany(
            "INSERT INTO consents (customer_id, channel, status,"
            " captured_by) SELECT c.id, $2, $3, 'reseed-recent'"
            " FROM customers c WHERE c.member_id = $1"
            " ON CONFLICT DO NOTHING",
            consent_rows)
        await conn.execute(
            "INSERT INTO consent_events"
            " (customer_id, channel, status, captured_by)"
            " SELECT customer_id, channel, status, captured_by"
            " FROM consents WHERE captured_by = 'reseed-recent'"
            " ON CONFLICT DO NOTHING")
        # One first order each, near signup (clamped to the window).
        for c in cust_rows:
            m = c[0]
            mrng = _member_rng(seed, 303, m)
            signup = c[2].date()
            od = min(signup + timedelta(days=mrng.randint(0, 10)),
                     RECENT_END)
            ordered = datetime(od.year, od.month, od.day,
                               mrng.randint(9, 20), mrng.randint(0, 59),
                               tzinfo=timezone.utc)
            seqn = int(m[2:])
            ext = f"{RECENT_SOURCE_TAG}-{seqn}-01"
            if ext in have_ext:
                continue
            store = mrng.choices(store_ids, weights=store_w)[0]
            lines, total = _basket_lines(mrng, pmap, skus, weights)
            signup_payback.append((idmap[m], store, ordered, total, ext))
            signup_items.extend([(ext, pid, qty, price)
                                 for (pid, qty, price) in lines])
    payback.extend(signup_payback)
    item_rows.extend(signup_items)
    if payback:
        await conn.executemany(
            "INSERT INTO orders (customer_id, store_id, ordered_at,"
            " total_cents, status, external_order_id)"
            " VALUES ($1,$2,$3,$4,'completed',$5)", payback)
        oid = {r["external_order_id"]: str(r["id"])
               for r in await conn.fetch(
                   "SELECT id, external_order_id FROM orders"
                   " WHERE external_order_id = ANY($1)",
                   [p[4] for p in payback])}
        await conn.executemany(
            "INSERT INTO order_items (order_id, product_id, qty,"
            " unit_price_cents) VALUES ($1,$2,$3,$4)",
            [(oid[ext], pid, qty, price)
             for (ext, pid, qty, price) in item_rows])
    return {"members_hit": members_hit, "signups_added": signups_added,
            "orders": len(payback), "items": len(item_rows)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url",
                    default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--wipe-analytics", action="store_true",
                    help="TRUNCATE analytics tables before seeding")
    ap.add_argument("--reset-seed-block", action="store_true",
                    help="delete this seeder's own rows (M-03% members,"
                         " RS3-% orders) first, so a distribution change"
                         " reaches an existing dataset (implies"
                         " --wipe-analytics)")
    args = ap.parse_args()
    if not args.database_url:
        print("set --database-url (never default a seeder at a live DB)",
              file=sys.stderr)
        return 2
    if args.reset_seed_block and not args.wipe_analytics:
        print("reset: --reset-seed-block implies --wipe-analytics"
              " (analytics rows reference the removed customers)")
        args.wipe_analytics = True

    rng = random.Random(args.seed)
    conn = await asyncpg.connect(args.database_url)
    try:
        if args.wipe_analytics:
            for t in ANALYTICS_TABLES:
                exists = await conn.fetchval(
                    "SELECT to_regclass($1)", f"public.{t}")
                if exists is None:
                    print(f"wipe: {t} absent, skip")
                    continue
                await conn.execute(f"TRUNCATE {t} CASCADE")
                print(f"wipe: TRUNCATE {t} CASCADE")
        if args.reset_seed_block:
            await _reset_seed_block(conn)
        store_rows = await conn.fetch(
            "SELECT s.id, s.code, b.code AS brand FROM stores s"
            " JOIN brands b ON b.id = s.brand_id ORDER BY s.code")
        if not store_rows:
            print("no stores: run bootstrap first", file=sys.stderr)
            return 2
        per_brand: dict[str, int] = {}
        for r in store_rows:
            per_brand[r["brand"]] = per_brand.get(r["brand"], 0) + 1
        store_ids = [r["id"] for r in store_rows]
        store_w = [BRAND_SHARE.get(r["brand"], 0.05) / per_brand[r["brand"]]
                   for r in store_rows]
        prods = await conn.fetch(
            "SELECT id, sku, category, price_cents FROM products")
        if not prods:
            print("no products: run bootstrap first", file=sys.stderr)
            return 2
        pmap = {r["sku"]: dict(r) for r in prods}
        cat_of = {r["sku"]: r["category"] for r in prods}
        skus = [r["sku"] for r in prods]
        weights = [CAT_W.get(cat_of[s], 1.0) for s in skus]
        grp = await conn.fetchval(
            "SELECT id FROM groups WHERE code = 'DEMO'")
        tiers = {r["code"] for r in await conn.fetch(
            "SELECT code FROM membership_tiers")}
        use_tiers = {"PLATINUM", "GOLD", "SILVER"} <= tiers

        members = [f"M-{MEMBER_SEQ_BASE + i:06d}"
                   for i in range(1, N_MEMBERS + 1)]
        have = {r["member_id"] for r in await conn.fetch(
            "SELECT member_id FROM customers WHERE member_id = ANY($1)",
            members)}
        new_members = [m for m in members if m not in have]
        print(f"members: {len(members)} total, "
              f"{len(new_members)} to insert")

        cust_rows, contact_rows = [], []
        consent_rows, event_rows = [], []
        order_plan: list[tuple] = []  # (member_idx, signup, last, n)
        used_phones = {r["value_normalized"] for r in await conn.fetch(
            "SELECT value_normalized FROM customer_contacts"
            " WHERE kind = 'phone'")}
        for idx, m in enumerate(new_members):
            signup = _draw_signup(rng)
            last = signup
            rec = _draw_recency_days(rng)
            last_order = min(ANCHOR - timedelta(days=rec), ANCHOR)
            if last_order < signup:
                last_order = signup
            vip = rng.random() < 0.20
            n = max(int(rng.lognormvariate(3.3, 0.6)) if vip
                    else int(rng.lognormvariate(0.5, 0.8)), 0)
            n = min(n, 60)
            # Cohort ladder: the first order must land near signup, so a
            # buyer needs a 2nd order to carry the drawn recency. Only
            # just-signed-up (<=45d) members may end at a single order.
            if n > 0 and (last_order - signup).days > 45:
                n = max(n, 2)
            age = min(max(int(rng.gauss(38, 12)), 18), 80)
            dob = ANCHOR - timedelta(days=int(age * 365.25))
            if rng.random() < 0.12:
                name = rng.choice(WESTERN)
                locale = "en"
            else:
                name = (f"{rng.choice(SURNAMES)} "
                        f"{rng.choice(GIVEN)}")
                locale = "zh-HK"
            created = datetime(signup.year, signup.month, signup.day,
                               10, 0, tzinfo=timezone.utc)
            cust_rows.append((m, name, created, dob,
                             rng.choice(["male", "female"]),
                             rng.choice(DISTRICTS), locale, grp))
            seqn = MEMBER_SEQ_BASE + members.index(m) + 1
            nph = 90000000 + (seqn * 7919) % 9999999
            phone = f"+852{nph:08d}"
            while phone in used_phones:
                nph += 1
                phone = f"+852{nph:08d}"
            used_phones.add(phone)
            order_plan.append((m, signup, last_order, n,
                               phone, seqn, name,
                               rng.choices(store_ids, weights=store_w)[0]))
        if cust_rows:
            await conn.executemany(
                "INSERT INTO customers (member_id, full_name, created_at,"
                " date_of_birth, gender, district, attributes, group_id)"
                " VALUES ($1,$2,$3,$4,$5,$6,"
                " jsonb_build_object('locale', $7::text), $8::uuid)",
                cust_rows)
        idmap = {r["member_id"]: str(r["id"]) for r in await conn.fetch(
            "SELECT id, member_id FROM customers"
            " WHERE member_id = ANY($1)", new_members)}
        have_ext = {r["external_order_id"] for r in await conn.fetch(
            "SELECT external_order_id FROM orders"
            " WHERE external_order_id LIKE $1", f"{SOURCE_TAG}-%")}

        payback, item_rows, spend = [], [], {}
        for (m, signup, last_order, n, phone, seqn, name,
             home) in order_plan:
            cid = idmap[m]
            local = name.lower().replace(" ", ".")
            contact_rows.append((cid, "phone", phone,
                                f"+852 {phone[4:8]} {phone[8:]}",
                                True))
            email = f"{local}.{seqn}@example.com"
            contact_rows.append((cid, "email", email, email, False))
            optout = rng.random() < 0.08
            for ch in CHANNELS:
                if optout:
                    st = "opted_out"
                elif ch == "sms":
                    st = ("opted_in" if rng.random() < 0.90
                          else "opted_out")
                elif ch == "email":
                    st = ("opted_in" if rng.random() < 0.85
                          else "opted_out")
                else:
                    st = ("opted_in" if rng.random() < 0.40
                          else "opted_out")
                consent_rows.append((cid, ch, st, "reseed"))
            total_spend = 0
            for k in range(n):
                if k == 0 and n > 1:
                    # First order soon after signup: sets the cohort
                    # month (020) to ~signup month, spreading cohorts
                    # across the whole signup window.
                    od = min(signup + timedelta(days=rng.randint(0, 45)),
                             last_order)
                elif k == n - 1:
                    # Pin the final order at the window end so actual
                    # recency equals the drawn recency (otherwise sparse
                    # orders drift older than their band).
                    od = last_order
                else:
                    od = _draw_order_date(rng, signup, last_order)
                ordered = datetime(od.year, od.month, od.day,
                                   rng.randint(9, 20),
                                   rng.randint(0, 59),
                                   tzinfo=timezone.utc)
                store = (home if rng.random() < HOME_STORE_P
                         else rng.choices(store_ids, weights=store_w)[0])
                ext = f"{SOURCE_TAG}-{seqn}-{k + 1:03d}"
                if ext in have_ext:
                    continue
                lines, total = [], 0
                nlines = 1 + (1 if rng.random() < 0.45 else 0) \
                    + (1 if rng.random() < 0.15 else 0)
                for _ in range(nlines):
                    sku = rng.choices(skus, weights=weights)[0]
                    qty = 1 + (1 if rng.random() < 0.25 else 0) \
                        + (1 if rng.random() < 0.05 else 0)
                    price = pmap[sku]["price_cents"]
                    total += price * qty
                    lines.append((ext, pmap[sku]["id"], qty, price))
                payback.append((cid, store, ordered, total, ext))
                item_rows.extend(lines)
                total_spend += total
            spend[cid] = total_spend
        if payback:
            await conn.executemany(
                "INSERT INTO orders (customer_id, store_id, ordered_at,"
                " total_cents, status, external_order_id)"
                " VALUES ($1,$2,$3,$4,'completed',$5)", payback)
            oid = {r["external_order_id"]: str(r["id"])
                   for r in await conn.fetch(
                       "SELECT id, external_order_id FROM orders"
                       " WHERE external_order_id = ANY($1)",
                       [p[4] for p in payback])}
            await conn.executemany(
                "INSERT INTO order_items (order_id, product_id, qty,"
                " unit_price_cents) VALUES ($1,$2,$3,$4)",
                [(oid[ext], pid, qty, price)
                 for (ext, pid, qty, price) in item_rows])
        if contact_rows:
            await conn.executemany(
                "INSERT INTO customer_contacts (customer_id, kind,"
                " value_normalized, value_display, is_primary)"
                " VALUES ($1,$2,$3,$4,$5)", contact_rows)
        if consent_rows:
            await conn.executemany(
                "INSERT INTO consents (customer_id, channel, status,"
                " captured_by) VALUES ($1,$2,$3,$4)"
                " ON CONFLICT DO NOTHING", consent_rows)
            await conn.execute(
                "INSERT INTO consent_events"
                " (customer_id, channel, status, captured_by)"
                " SELECT customer_id, channel, status, captured_by"
                " FROM consents WHERE captured_by = 'reseed'"
                " ON CONFLICT DO NOTHING")
        if use_tiers and spend:
            ranked = sorted(spend.items(), key=lambda kv: kv[1],
                            reverse=True)
            n = len(ranked)
            for i, (cid, _) in enumerate(ranked):
                tier = ("PLATINUM" if i < n // 10
                        else "GOLD" if i < n * 35 // 100
                        else "SILVER" if rng.random() < 0.5 else None)
                if tier:
                    await conn.execute(
                        "UPDATE customers SET tier_code = $1"
                        " WHERE id = $2 AND tier_code IS NULL",
                        tier, cid)
        # --- recent fill 2026-10-16..2026-10-30 (additive) ---
        # Extra completed orders for a deterministic M-03 subset plus
        # in-window signups; skip-if-exists in Python, so a rerun is a
        # no-op. Runs BEFORE the channel fill so new signups get a
        # channel too.
        recent = await _recent_fill(conn, args.seed, store_ids, store_w,
                                    pmap, skus, weights, grp)
        print(f"recent: {recent['members_hit']} member(s) +"
              f" {recent['signups_added']} new signup(s),"
              f" {recent['orders']} order(s) {RECENT_START}..{RECENT_END}")
        # --- acquisition-channel fill (additive, M-03% unknowns only) ---
        n_m03, n_chan = await _assign_channels(conn, args.seed)
        print(f"channels: {n_m03} M-03% member(s),"
              f" {n_chan} newly assigned")
        # --- catalog campaign membership: deterministic hash slice ---
        # App decision: catalog campaigns holding a handful of members
        # read as broken on the campaigns page, so each one gets a
        # 120-300 member slice of THIS seeder's members. Independent
        # per-campaign rankings -> slices overlap. Strictly additive:
        # demo/bootstrap campaigns are not in the code list and the
        # INSERT never deletes. Re-run => same rows (ON CONFLICT).
        camps = await conn.fetch(
            "SELECT id, code FROM campaigns WHERE code = ANY($1)"
            " ORDER BY code", list(CATALOG_CAMPAIGN_CODES))
        mem_of = {r["member_id"]: str(r["id"]) for r in await conn.fetch(
            "SELECT id, member_id FROM customers"
            " WHERE member_id = ANY($1)", members)}
        if camps and mem_of:
            slice_rows = []
            for c in camps:
                ranked = sorted(mem_of.items(),
                                key=lambda kv: _campaign_slice_key(
                                    c["code"], kv[0]))
                for _mid, cid in ranked[:_campaign_slice_size(c["code"])]:
                    slice_rows.append((str(c["id"]), cid, "reseed"))
            await conn.executemany(
                "INSERT INTO campaign_members (campaign_id, customer_id,"
                " assigned_by) VALUES ($1::uuid, $2::uuid, $3)"
                " ON CONFLICT (campaign_id, customer_id) DO NOTHING",
                slice_rows)
            per = {c["code"]: _campaign_slice_size(c["code"])
                   for c in camps}
            print(f"campaign_members: {len(camps)} catalog campaign(s), "
                  f"{len(slice_rows)} slice row(s) "
                  f"targets={per}")
        span = await conn.fetchrow(
            "SELECT min(ordered_at)::date lo, max(ordered_at)::date hi,"
            " count(*) n, coalesce(sum(total_cents),0) rev FROM orders")
        top = await conn.fetch(
            "SELECT customer_id, sum(total_cents) s FROM orders"
            " GROUP BY 1 ORDER BY 2 DESC")
        top20n = max(len(top) // 5, 1)
        share = (sum(r["s"] for r in top[:top20n]) /
                 max(span["rev"], 1))
        rec = await conn.fetchval(
            "SELECT max(CURRENT_DATE - max_od)::int FROM"
            " (SELECT customer_id, max(ordered_at)::date max_od"
            " FROM orders GROUP BY 1) t")
        lad = await conn.fetchrow(
            "SELECT count(DISTINCT date_trunc('month', f)) m,"
            " min(f)::date lo FROM (SELECT customer_id, min(ordered_at) f"
            " FROM orders GROUP BY 1) t")
        optout = await conn.fetchval(
            "SELECT count(*) FROM (SELECT customer_id FROM consents"
            " GROUP BY 1 HAVING bool_and(status = 'opted_out')) t")
        ncust = await conn.fetchval("SELECT count(*) FROM customers")
        print(f"customers={ncust} orders={span['n']} "
              f"range={span['lo']}..{span['hi']} "
              f"cohort_months={lad['m']} first_cohort={lad['lo']} "
              f"top20_share={share:.2f} max_recency_d={rec} "
              f"full_optout={optout}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
