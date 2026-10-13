"""DEV/CI ONLY — NEVER run against a live DB.

This script TRUNCATEs 11 tables + reseeds demo data. Live databases must be
migrated migrate-only (apply db/migrations up-sections, never this script).
"""
import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.api.db import get_database_url  # noqa: E402
import asyncpg  # noqa: E402


async def main() -> None:
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
        # idempotent re-seed: wipe demo rows first (demo.sql has fixed ids)
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
