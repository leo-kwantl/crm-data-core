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
# Append-only tables declare their immutability as a grant revocation, next to
# the CREATE TABLE (027, 029, 037). A `REVOKE` is DDL that no `CREATE TABLE`
# check can see, and it is skippable the same way a column is: a cluster that
# recorded the version before the line was added never runs it, and the table
# looks identical either way.
REVOKE_RE = re.compile(
    r"REVOKE\s+UPDATE\s*,\s*DELETE\s+ON\s+([a-z_][a-z0-9_]*)", re.I)

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


def append_only_tables() -> set[str]:
    """Tables whose migrations revoke UPDATE/DELETE from PUBLIC."""
    tables: set[str] = set()
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = STRING_RE.sub("", DO_BLOCK_RE.sub("", _up_sql(mig)))
        tables.update(m.group(1).lower() for m in REVOKE_RE.finditer(sql))
    return tables


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


async def test_append_only_revokes_ran_on_this_database(
        database_url: str) -> None:
    """A table whose REVOKE never ran still looks append-only by name only.

    `relacl` is NULL while a table keeps default privileges, so an un-applied
    revoke is invisible to the declared-object check above: after
    `REVOKE UPDATE, DELETE ... FROM PUBLIC` PostgreSQL stores an explicit ACL
    (the owner's entry, without the PUBLIC one). Either signal -- NULL acl, or
    an ACL still granting PUBLIC UPDATE/DELETE -- means the line did not reach
    this database.
    """
    tables = append_only_tables()
    assert tables, "no REVOKE UPDATE, DELETE statement found in db/migrations"
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            "SELECT c.relname, c.relacl::text AS acl,"
            "       e.grantee::regrole::text AS grantee,"
            "       e.privilege_type AS priv"
            "  FROM pg_class c"
            "  JOIN pg_namespace n ON n.oid = c.relnamespace"
            "  LEFT JOIN LATERAL aclexplode(c.relacl) e ON TRUE"
            " WHERE c.relkind = 'r' AND n.nspname = 'public'"
            "   AND c.relname = ANY($1::text[])", sorted(tables))
    finally:
        await conn.close()
    seen = {r["relname"] for r in rows}
    assert not (tables - seen), f"append-only tables missing: {sorted(tables - seen)}"
    acl_null = sorted(r["relname"] for r in rows if r["acl"] is None)
    public_rw = sorted(
        f"{r['relname']}:{r['priv']}" for r in rows
        if r["grantee"] == "-" and r["priv"] in ("UPDATE", "DELETE"))
    assert not acl_null, (
        "append-only table still has default privileges, so its "
        f"REVOKE UPDATE, DELETE never ran here: {acl_null}")
    assert not public_rw, f"PUBLIC still holds UPDATE/DELETE on: {public_rw}"


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


MODEL_CHECK_RE = re.compile(r"CHECK\s*\(\s*model\s+IN\s*\(([^)]*)\)", re.I)

# The two models ml's trust-layer writers record quality rows for; 042 exists
# because the table rejected them outright. A third model added there has to be
# added here too -- this is the data-side half of that pair.
REQUIRED_TRAINING_RUN_MODELS = {"forecast", "assoc"}


def declared_training_run_models() -> set[str]:
    """The `model_training_runs.model` whitelist as the migrations last set it.

    Table-scoped on purpose: `028`/`034` declare their own `CHECK (model IN ...)`
    on `btyd_params`, so a bare regex would read the wrong table's list. Only
    statements that name `model_training_runs` are considered, and the last
    declaration wins (013 creates it, 042 widens it).
    """
    declared: set[str] = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        up = UP_RE.search(path.read_text(encoding="utf-8"))
        if not up:
            continue
        for statement in up.group(1).split(";"):
            if "model_training_runs" not in statement:
                continue
            match = MODEL_CHECK_RE.search(statement)
            if match:
                declared = {value.strip().strip("'")
                            for value in match.group(1).split(",") if value.strip()}
    return declared


async def test_declared_training_run_models_are_writable(
        database_url: str) -> None:
    """042's widening, checked as behaviour rather than as constraint text.

    Every value the migrations declare must actually insert -- `forecast` and
    `assoc` could not, so ml had no lineage for two models -- and the check must
    still reject a value nobody declares. Rolled back: this writes nothing.

    Not covered here: that `ml_engine/training_runs.py` `MODELS` matches this
    list. That is a cross-repo pair, so it is the writer's suite that pins it.
    """
    declared = declared_training_run_models()
    assert declared, "no model_training_runs model whitelist found in db/migrations"
    assert REQUIRED_TRAINING_RUN_MODELS <= declared, (
        "model_training_runs rejects a model whose writer records quality rows,"
        f" so those rows cannot be written at all: missing"
        f" {sorted(REQUIRED_TRAINING_RUN_MODELS - declared)}")
    conn = await asyncpg.connect(database_url)
    try:
        tr = conn.transaction()
        await tr.start()
        for model in sorted(declared):
            await conn.execute(
                "INSERT INTO model_training_runs (model) VALUES ($1)", model)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO model_training_runs (model) VALUES ('not_a_model')")
        await tr.rollback()
    finally:
        await conn.close()
