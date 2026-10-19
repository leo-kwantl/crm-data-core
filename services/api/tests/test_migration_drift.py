"""Guard: the connected database must contain what db/migrations declares.

The 2026-10-18 review (§4.1) found the demo cluster holding 36 migration
versions, head `034`, while `orders.source` did not exist: migration
`014_upload_staging.sql` gained that column (and `orders_source_ext_ux`) on
2026-10-15, after the cluster's data directory had already recorded version
014. dbmate and `scripts/bootstrap_db.py` both skip a version present in
`schema_migrations`, so an edit to an already-applied migration lands on
nobody -- and a from-scratch test database cannot see it, because it replays
every file's current text by definition.

So this test does what the review's blast-radius probe did, as a test:

1. parse the up-section of every migration for the objects it declares
   (tables, added columns, indexes) and assert each exists in the connected
   database;
2. `PREPARE` every module-level SQL constant under `pipelines/**` and assert
   it resolves against that database (a statement naming a column the
   migrations never create fails here).

In CI the first test is the invariant; pointed at a long-lived cluster it is
a drift detector:

    DATABASE_URL=postgresql://... pytest services/api/tests/test_migration_drift.py
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import asyncpg
import pytest

# Own short-lived connections per test (conftest's session pool can live on a
# different event loop); `database_url` is the only shared fixture needed.
pytestmark = pytest.mark.asyncio

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"
UP_RE = re.compile(r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)

CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)", re.I)
ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([a-z_][a-z0-9_]*)"
    r"\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)", re.I)
CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([a-z_][a-z0-9_]*)", re.I)

# Clauses, not statements: interpolated into other SQL, so PREPARE cannot run
# them alone. The statements that embed them are checked on their own.
FRAGMENT_CONSTANTS = {"pipelines.snapshot.BUSINESS_TZ_SQL"}

# DDL inside a DO block is conditional by design (the pg_trgm GIN index exists
# only where the extension does, with a btree fallback otherwise), so a
# declared-object check must not demand both branches. The block and its
# quoted EXECUTE literals are removed before parsing.
DO_BLOCK_RE = re.compile(r"DO\s+\$\$.*?\$\$\s*;", re.S | re.I)
STRING_RE = re.compile(r"'(?:[^']|'')*'")


def _up_sql(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = UP_RE.search(text)
    return m.group(1) if m else ""


def declared_objects() -> tuple[set[str], set[tuple[str, str]], set[str]]:
    """Every table / (table, column) / index the up-sections declare."""
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    indexes: set[str] = set()
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = STRING_RE.sub("", DO_BLOCK_RE.sub("", _up_sql(mig)))
        tables.update(m.group(1).lower() for m in CREATE_TABLE_RE.finditer(sql))
        columns.update((m.group(1).lower(), m.group(2).lower())
                       for m in ADD_COLUMN_RE.finditer(sql))
        indexes.update(m.group(1).lower()
                       for m in CREATE_INDEX_RE.finditer(sql))
    return tables, columns, indexes


def sql_constants() -> dict[str, str]:
    """Module-level `*_SQL` strings across the pipelines package."""
    import pipelines

    found: dict[str, str] = {}
    for mod in pkgutil.walk_packages(pipelines.__path__, "pipelines."):
        module = importlib.import_module(mod.name)
        for name in dir(module):
            if not name.endswith("_SQL"):
                continue
            val = getattr(module, name)
            key = f"{mod.name}.{name}"
            if isinstance(val, str) and key not in FRAGMENT_CONSTANTS:
                found[key] = val
    return found


async def test_declared_ddl_exists_in_database(database_url: str) -> None:
    tables, columns, indexes = declared_objects()
    assert tables and columns and indexes, "migration parser found nothing"
    conn = await asyncpg.connect(database_url)
    try:
        live_tables = {r["relname"] for r in await conn.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n"
            " ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND n.nspname = 'public'")}
        live_columns = {(r["table_name"], r["column_name"])
                        for r in await conn.fetch(
                            "SELECT table_name, column_name"
                            " FROM information_schema.columns"
                            " WHERE table_schema = 'public'")}
        live_indexes = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = 'public'")}
    finally:
        await conn.close()
    missing_tables = sorted(tables - live_tables)
    missing_columns = sorted(columns - live_columns)
    missing_indexes = sorted(indexes - live_indexes)
    assert not missing_tables, f"declared tables missing: {missing_tables}"
    assert not missing_columns, (
        "declared columns missing (an applied migration was edited after this"
        f" database ran it): {missing_columns}")
    assert not missing_indexes, (
        "declared indexes missing (an applied migration was edited after this"
        f" database ran it): {missing_indexes}")


async def test_pipelines_statements_resolve_against_database(
        database_url: str) -> None:
    consts = sql_constants()
    assert len(consts) > 40, f"only {len(consts)} statements found"
    failures: list[str] = []
    conn = await asyncpg.connect(database_url)
    try:
        for key, sql in sorted(consts.items()):
            body = sql.strip().rstrip(";").strip()
            try:
                await conn.execute(f"PREPARE _prepared AS {body}")
            except Exception as exc:  # noqa: BLE001 - reported below
                failures.append(f"{key}: {type(exc).__name__}: {exc}")
            finally:
                await conn.execute("DEALLOCATE ALL")
    finally:
        await conn.close()
    assert not failures, (
        "statements do not resolve against this database:\n"
        + "\n".join(failures))
