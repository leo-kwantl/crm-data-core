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

Children are deleted before parents (the foreign keys are NO ACTION), and every
row matched is written to a JSON dump before anything is removed.
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    for flag, _, _ in FAMILIES:
        ap.add_argument(f"--{flag.replace('_', '-')}", action="append",
                        default=[], dest=flag)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    wanted = {flag: getattr(args, flag) for flag, _, _ in FAMILIES}
    if not any(wanted.values()):
        raise SystemExit(
            "nothing named -- pass at least one --member/--sku/--store/"
            "--connector/--upload-file (there is no 'everything else' mode)")

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
            matched[table] = [dict(r) for r in rows]
            print(f"{table:<14} {len(rows):4d} matched by {column} "
                  f"({len(values)} named)")
            for row in matched[table]:
                rest = "  ".join(str(v) for k, v in row.items()
                                 if k not in ("id", column))
                print(f"   {row[column]:<14} {rest}")

        missing = [
            v for flag, table, column in FAMILIES for v in wanted[flag]
            if v not in {row[column] for row in matched.get(table, [])}]
        if missing:
            print("named but absent:", ", ".join(missing))

        if not args.apply:
            print("\ndry run -- pass --apply to delete the rows listed above")
            return

        out = pathlib.Path(".pi") / f"debris-{int(time.time())}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(matched, indent=2), encoding="utf-8")
        print("dumped:", out)

        customer_ids = [r["id"] for r in matched.pop("customers", [])]
        if customer_ids:
            for table, where in CHILD_DELETES:
                tag = await conn.execute(
                    f"DELETE FROM {table} WHERE {where}", customer_ids)
                print(f"  {table:<22} {tag}")
            tag = await conn.execute(
                "DELETE FROM customers WHERE id = ANY($1::uuid[])", customer_ids)
            print(f"  {'customers':<22} {tag}")
        for flag, table, column in FAMILIES:
            if table == "customers" or not wanted[flag]:
                continue
            tag = await conn.execute(
                f"DELETE FROM {table} WHERE {column} = ANY($1::text[])",
                wanted[flag])
            print(f"  {table:<22} {tag}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
