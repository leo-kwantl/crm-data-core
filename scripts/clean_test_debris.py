"""Delete named test debris from a dev database -- dry run by default.

Every row this removes is named on the command line. There is deliberately no
"outside the seed space" mode: a predicate by exclusion cannot tell a test's
leftover from a person's manual fixture, and the 2026-10-20 incident was a
purge being *considered* against ids `db/seeds/demo.sql` owns. If a row is not
worth typing here, it is not worth deleting.

The target comes from `DATABASE_URL` only -- no default DSN, because a purge
aimed at the wrong cluster is the one action in this repo that re-running does
not fix. The resolved target is printed as `host:port/db`, never with a user or
password.

    set DATABASE_URL=postgresql://...                 (cmd: no `export`)
    python scripts/clean_test_debris.py --member M-900123 --upload-file c1.csv
    python scripts/clean_test_debris.py --member M-900123 --apply

A family can be named by VALUE (--member M-900123) or by SHAPE (--staff,
--user, --store-name): the shape families are the ones a foreign test suite
leaks, where the operator cannot know the id but the fixture's name is the
signature (`Test Staff <hash>`, `u-<hash>`, `Store <n>`). A shape family still
names its predicate on the command line -- the pattern is printed before the
dry run, so the reader sees exactly which rows are in scope.

Children are deleted before parents (the foreign keys are NO ACTION), and every
row matched is written to a JSON dump before anything is removed. Users are
deleted before staff (users.staff_id is ON DELETE RESTRICT) and staff before
stores (staff.store_id). A store that still has orders, stock or staff is NOT
deleted: it is reported instead, because a live store is a human's decision.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import time

import asyncpg

# (label, table, column, argument attribute) -- one DELETE per named family.
FAMILIES = (
    ("member", "customers", "member_id"),
    ("sku", "products", "sku"),
    ("store", "stores", "code"),
    ("connector", "connectors", "name"),
    ("upload_file", "upload_jobs", "filename"),
)

# Shape families: (flag, table, column, pattern) -- the flag selects the
# SHAPE, the pattern is the SQL LIKE pattern that names the fixture. The
# default pattern is the one the foreign suite uses today; `--store-name` may
# carry its own pattern (e.g. 'Cohort Test Store').
PATTERN_FAMILIES = (
    ("staff", "staff", "full_name", "Test Staff %"),
    ("user", "users", "username", "u-%"),
    ("store_name", "stores", "name", "Store %"),
)

# Children of a matched customer / of a matched order, deepest first.
CHILD_DELETES = (
    ("order_custom_data", "order_id IN (SELECT id FROM orders WHERE"
     " customer_id = ANY($1::uuid[]))"),
    ("order_items", "order_id IN (SELECT id FROM orders WHERE"
     " customer_id = ANY($1::uuid[]))"),
    ("orders", "customer_id = ANY($1::uuid[])"),
    ("customer_events", "customer_id = ANY($1::uuid[])"),
    ("consent_events", "customer_id = ANY($1::uuid[])"),
    ("customer_contacts", "customer_id = ANY($1::uuid[])"),
    ("customer_access_logs", "customer_id = ANY($1::uuid[])"),
)


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set -- refusing to guess which cluster to "
            "purge (this tool deletes rows).")
    print("target:", url.split("@")[-1])
    return url


async def _store_dependents(conn, store_ids: list[str]) -> list[dict]:
    """Orders / stock / staff still hanging off these stores."""
    rows = await conn.fetch(
        "SELECT s.id::text AS id, s.code, s.name,"
        " (SELECT count(*) FROM orders o WHERE o.store_id = s.id) AS orders,"
        " (SELECT count(*) FROM inventory i WHERE i.store_id = s.id) AS stock,"
        " (SELECT count(*) FROM staff t WHERE t.store_id = s.id) AS staff"
        " FROM stores s WHERE s.id = ANY($1::uuid[]) ORDER BY s.code",
        store_ids)
    return [dict(r) for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delete named test debris from a dev database "
                    "(dry run unless --apply).")
    for flag, _, _ in FAMILIES:
        ap.add_argument(f"--{flag.replace('_', '-')}", action="append",
                        default=[], dest=flag)
    for flag, table, column, default_pattern in PATTERN_FAMILIES:
        ap.add_argument(f"--{flag.replace('_', '-')}", nargs="?",
                        const=default_pattern, default=None, metavar="PATTERN",
                        help=f"{table} rows whose {column} matches PATTERN "
                             f"(default {default_pattern.replace('%', '%%')!r})")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    wanted = {flag: getattr(args, flag) for flag, _, _ in FAMILIES}
    # Shape families: the flag names the fixture's SHAPE, the pattern names
    # the rows. The pattern is printed before the dry run, so the reader can
    # see exactly which rows the shape covers.
    shapes: dict[str, tuple[str, str, str]] = {}
    for flag, table, column, default_pattern in PATTERN_FAMILIES:
        value = getattr(args, flag, None)
        if value:
            shapes[flag] = (table, column, value)
    if not any(wanted.values()) and not shapes:
        raise SystemExit(
            "nothing named -- pass at least one --member/--sku/--store/"
            "--connector/--upload-file/--staff/--user/--store-name "
            "(there is no 'everything else' mode)")

    conn = await asyncpg.connect(dsn())
    try:
        matched: dict[str, list[dict]] = {}
        for flag, table, column in FAMILIES:
            values = wanted[flag]
            if not values:
                continue
            # `created_at` alone cannot separate a fixture from a real customer
            # onboarded the same day; the name is the signature (`Ext User
            # <hash>`), so print it and let the reader check the rule.
            extra = {"customers": ", full_name, created_at::text"}.get(
                table, "" if table == "products" else ", created_at::text")
            rows = await conn.fetch(
                f"SELECT id::text, {column}{extra} FROM {table}"
                f" WHERE {column} = ANY($1::text[]) ORDER BY {column}", values)
            matched[flag] = [dict(r) for r in rows]
            print(f"{table:<14} {len(rows):4d} matched by {column} "
                  f"({len(values)} named)")
            for row in matched[flag]:
                rest = "  ".join(str(v) for k, v in row.items()
                                 if k not in ("id", column))
                print(f"   {row[column]:<14} {rest}")

        for flag, (table, column, pattern) in shapes.items():
            extra = ", full_name, created_at::text" if table == "staff" \
                else ", display_name, created_at::text" if table == "users" \
                else ", created_at::text"
            rows = await conn.fetch(
                f"SELECT id::text, {column}{extra} FROM {table}"
                f" WHERE {column} LIKE $1 ORDER BY {column}", pattern)
            matched[flag] = [dict(r) for r in rows]
            print(f"{table:<14} {len(rows):4d} matched by {column} LIKE "
                  f"{pattern!r} (shape family)")
            for row in matched[flag]:
                rest = "  ".join(str(v) for k, v in row.items()
                                 if k not in ("id", column))
                print(f"   {row[column]:<14} {rest}")

        missing = [
            v for flag, table, column in FAMILIES for v in wanted[flag]
            if v not in {row[column] for row in matched.get(flag, [])}]
        if missing:
            print("named but absent:", ", ".join(missing))

        if not args.apply:
            print("\ndry run -- pass --apply to delete the rows listed above")
            return

        out = pathlib.Path(".pi") / f"debris-{int(time.time())}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(matched, indent=2), encoding="utf-8")
        print("dumped:", out)

        # Children before parents. users -> staff (RESTRICT) and
        # staff -> stores are the two orders that bite.
        customer_ids = [r["id"] for r in matched.get("member", [])]
        if customer_ids:
            for table, where in CHILD_DELETES:
                tag = await conn.execute(
                    f"DELETE FROM {table} WHERE {where}", customer_ids)
                print(f"  {table:<22} {tag}")
            tag = await conn.execute(
                "DELETE FROM customers WHERE id = ANY($1::uuid[])", customer_ids)
            print(f"  {'customers':<22} {tag}")

        for flag in ("user", "staff"):
            rows = matched.get(flag, [])
            if not rows:
                continue
            table, column, _ = shapes[flag]
            ids = [r["id"] for r in rows]
            tag = await conn.execute(
                f"DELETE FROM {table} WHERE id = ANY($1::uuid[])", ids)
            print(f"  {table:<22} {tag}")

        for flag, table, column in FAMILIES:
            if table in ("customers", "stores") or not wanted[flag]:
                continue
            tag = await conn.execute(
                f"DELETE FROM {table} WHERE {column} = ANY($1::text[])",
                wanted[flag])
            print(f"  {table:<22} {tag}")

        # Stores last: a store with orders, stock or staff is a live store,
        # not debris. It is reported, never deleted.
        store_rows = matched.get("store", []) + matched.get("store_name", [])
        if store_rows:
            deps = await _store_dependents(
                conn, [r["id"] for r in store_rows])
            blocked = {d["id"] for d in deps
                       if d["orders"] or d["stock"] or d["staff"]}
            for d in deps:
                if d["id"] in blocked:
                    print(f"  stores SKIPPED {d['code']:<12} has "
                          f"{d['orders']} orders, {d['stock']} stock, "
                          f"{d['staff']} staff -- not debris")
            for flag, column in (("store", "code"), ("store_name", "name")):
                rows = [r for r in matched.get(flag, [])
                        if r["id"] not in blocked]
                if not rows:
                    continue
                tag = await conn.execute(
                    f"DELETE FROM stores WHERE {column} = ANY($1::text[])",
                    [r[column] for r in rows])
                print(f"  {'stores':<22} {tag}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
