"""Verify-holder: boot pgembed at .pgembed-verify, migrate, seed demo.sql, hold.

Usage: python scripts/verify_holder.py  (prints DATABASE_URL, then sleeps)
NEVER point this at .pgembed-data (live :8000 DB).
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UP_RE = re.compile(r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)


async def main() -> None:
    import asyncpg
    from pgembed import get_server

    data_dir = str(ROOT / ".pgembed-verify")
    server = get_server(data_dir)
    url = server.get_uri("postgres")
    pool = await asyncpg.create_pool(url)
    assert pool is not None
    async with pool.acquire() as conn:
        already = await conn.fetchval("SELECT to_regclass('public.customers')")
        if already is None:
            for mig in sorted((ROOT / "db" / "migrations").glob("*.sql")):
                m = UP_RE.search(mig.read_text(encoding="utf-8"))
                if m and m.group(1).strip():
                    await conn.execute(m.group(1).strip())
            seed = ROOT / "db" / "seeds" / "demo.sql"
            await conn.execute(seed.read_text(encoding="utf-8"))
        n_cust = await conn.fetchval("SELECT COUNT(*) FROM customers")
        n_ord = await conn.fetchval("SELECT COUNT(*) FROM orders")
    await pool.close()
    print(f"DATABASE_URL={url}", flush=True)
    print(f"seeded customers={n_cust} orders={n_ord}", flush=True)
    print("HOLDER_READY", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
