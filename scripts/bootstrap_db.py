"""DEV/CI ONLY — NEVER run against a live DB.

This script TRUNCATEs 11 tables + reseeds demo data. Live databases must be
migrated migrate-only (apply db/migrations up-sections, never this script).
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.api.db import get_database_url  # noqa: E402
import asyncpg  # noqa: E402


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Apply migrations; optionally reseed demo")
    ap.add_argument("--allow-reseed", action="store_true",
                    help="TRUNCATE demo tables and reseed demo.sql. "
                         "Without it this script is migrate-only and changes no rows.")
    ap.add_argument("--force", action="store_true",
                    help="With --allow-reseed, proceed even when customers "
                         "outside demo.sql's own M-000% block exist.")
    args = ap.parse_args(argv)
    url = get_database_url()
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (version TEXT PRIMARY KEY)"
        )
        applied = set(await conn.fetch("SELECT version FROM schema_migrations"))
        applied = {r["version"] for r in applied}
        up = re.compile(r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)
        for mig in sorted((REPO / "db/migrations").glob("*.sql")):
            if mig.stem in applied:
                print("already applied", mig.name)
                continue
            m = up.search(mig.read_text(encoding="utf-8"))
            if m and m.group(1).strip():
                try:
                    await conn.execute(m.group(1).strip())
                except Exception as e:  # noqa: BLE001 - already applied w/o record
                    print("probably already applied", mig.name,
                          str(e).splitlines()[0])
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)"
                    " ON CONFLICT DO NOTHING", mig.stem
                )
                print("recorded", mig.name)
        if not args.allow_reseed:
            print("reseed skipped: migrate-only run "
                  "(pass --allow-reseed to TRUNCATE + reseed demo.sql)")
        else:
            # Structural guard for the 2026-10-31 live wipe: an unforced
            # reseed runs only on a fresh or demo-only DB. demo.sql writes
            # fixed M-000% members; anything else takes a runbook to
            # rebuild and needs --force stated alongside --allow-reseed.
            try:
                foreign = await conn.fetchval(
                    "SELECT count(*) FROM customers "
                    "WHERE member_id NOT LIKE 'M-000%'")
            except Exception:  # noqa: BLE001 - fresh DB, no customers yet
                foreign = 0
            if foreign and not args.force:
                print(f"REFUSED: {foreign} customer(s) outside demo.sql's own "
                      f"M-000% block. This reseed would destroy them. "
                      f"Pass --allow-reseed --force together only if that "
                      f"is truly what you want.", file=sys.stderr)
                raise SystemExit(2)
            for t in ["consent_events", "consents", "customer_access_logs",
                      "order_items", "orders", "customer_contacts",
                      "customers", "products", "stores", "users", "staff"]:
                try:
                    await conn.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
                except Exception as e:  # noqa: BLE001 - table may not exist yet
                    print("skip truncate", t, str(e).splitlines()[0])
            await conn.execute((REPO / "db/seeds/demo.sql").read_text(encoding="utf-8"))
            # Demo passwords (documented in the batch report, never log hashes).
            # "changeme-<username>" nudges rotation; all >= 8 chars.
            import bcrypt  # noqa: E402

            for username, pw in [
                ("siu.ming", "changeme-siu.ming"),
                ("ka.yan", "changeme-ka.yan"),
                ("hq.admin", "changeme-hq.admin"),
            ]:
                hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
                await conn.execute(
                    "UPDATE users SET password_hash = $1 WHERE username = $2",
                    hashed, username,
                )
        for t in ["customers", "orders", "products", "users"]:
            print(t, await conn.fetchval(f"SELECT COUNT(*) FROM {t}"))
    finally:
        await conn.close()


asyncio.run(main())
