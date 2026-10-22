"""The shape of every group filter in data's writers and shared SQL.

WHY THIS FILE EXISTS
--------------------
A114 withdrew the group scope (product-decisions, 2026-10-22) and the
group cut that may follow is still UNPRICED, because a group scope only
means something if every statement that filters by group agrees on what
the group selects. ml pinned that shape on its own statements
(`ml_engine` + `tests/test_group_scope_predicate.py`, master `549ba79`)
and the pin is what found the disagreement: the customer-population
statements are conjunctive (the customer's group AND the brand's group)
and the brand-row facts are not.

This file is data's half of that pin. It says three things about every
group filter in data's tree:

1. The FORM of each filter. A population filter compares a group column
   with `=` (a bare `=` or the `($n::uuid IS NULL OR x.group_id = $n)`
   guard); a row's own scope column is compared with
   `IS NOT DISTINCT FROM` (a bare `=` there silently drops the GLOBAL
   scope -- the row with a NULL scope -- which is a scope-shaped row no
   reader can select). An UNQUALIFIED `group_id` -- the column of the
   statement's own table -- is a population filter when it is compared
   with `=` (which members of that group are in scope) and the row's own
   scope column when it is compared with `IS NOT DISTINCT FROM`.
2. The POPULATION each statement selects, as one of four families:
   - `conjunctive`      the customer's group AND the brand's group;
   - `customer-named`   the customer's group, the brand named by id;
   - `brand-row`        the brand's group only (no customer side);
   - `scope-exact`      the row's own scope column, no population.
3. That the pin is EXHAUSTIVE: a statement that filters by group and is
   not listed in `FAMILIES` fails the check, so a new group filter
   cannot join the tree unclassified.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not unify the families. The population a group selects is a
product question that A114 deferred (the price note: "the pair denotes
ONE population only if the two families agree on which one -- today they
do not, and the demo cannot say which is right"). This file freezes the
SHAPES that exist today and reports them; it does not choose between
them. Changing a family is a product decision, and the check is where
that change is recorded.

SUBJECT
-------
`pipelines/**` (data's writers -- the statements that produce
scope-shaped rows) and `db/queries/*.sql` (the canonical shared text).
The serving layer's runtime copies under `services/api/**` are NOT the
subject: they are app's code, mirrored here, and their shape is pinned
by the mirror guard (byte equality), not by this file. If the shared
file's scope convention is decided, this file's subject grows to the
runtime copies in that same change.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PIPELINES = ROOT / "pipelines"
QUERIES = ROOT / "db" / "queries"

# A comparison of a group column against a parameter. `IS NOT DISTINCT
# FROM` must come first in the alternation or `=` would match its tail.
GROUP_CONJUNCT_RE = re.compile(
    r"(?P<col>(?:[A-Za-z_]\w*\.)?group_id)\s*"
    r"(?P<op>IS\s+NOT\s+DISTINCT\s+FROM|IS\s+DISTINCT\s+FROM|!=|<>|=)\s*"
    r"(?P<param>\$\d+|:[A-Za-z_]\w*)",
    re.IGNORECASE,
)
# A comparison of a `groups` table's id against a parameter -- the
# brand's group, reached through the join `groups g ON g.id = b.group_id`.
GROUPS_ID_RE = re.compile(
    r"(?P<col>(?:[A-Za-z_]\w*\.)?id)\s*"
    r"(?P<op>IS\s+NOT\s+DISTINCT\s+FROM|IS\s+DISTINCT\s+FROM|!=|<>|=)\s*"
    r"(?P<param>\$\d+|:[A-Za-z_]\w*)",
    re.IGNORECASE,
)
FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
    re.IGNORECASE,
)
GROUPS_JOIN_RE = re.compile(
    r"\bJOIN\s+groups\s+(?:AS\s+)?([A-Za-z_]\w*)\s+ON\s+"
    r"\1\s*\.\s*id\s*=\s*([A-Za-z_]\w*)\s*\.\s*group_id",
    re.IGNORECASE,
)
INSERT_INTO_RE = re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_]\w*)", re.IGNORECASE)
BRAND_ID_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*brand_id\s*=", re.IGNORECASE)
COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
# The name a shared statement carries in its own comment block -- a
# stable anchor, so a statement added to the file does not renumber the
# ones that are already pinned.
STATEMENT_NAME_RE = re.compile(r"--\s*name:\s*([A-Za-z_]\w*)", re.IGNORECASE)
# A CTE's name: a FROM of a CTE is not a table.
CTE_NAME_RE = re.compile(
    r"(?:\bWITH|,)\s*([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)

# The families a group-filtering statement can belong to. The value is
# the family; the key is the statement (module::name for a Python
# constant, db/queries/<file>#<n> for a statement in a shared file).
#
# Every statement that filters by group MUST appear here. A statement
# that does not is a hole in the pin, and the check says so by name.
FAMILIES = {
    # --- data's writers (pipelines/**) -------------------------------
    # The KPI population: the customer's group AND the brand's group,
    # both read with the same scope parameter ($3). The predicates live
    # in the scope CTEs (KPI_DAILY_CTES), which the writer interpolates.
    "pipelines/snapshot.py::KPI_DAILY_CTES": "conjunctive",
    # The cohort population: the customer's group AND the brand's group
    # ($4). The third conjunct is the cohort row's own scope column.
    "pipelines/snapshot.py::COHORT_SNAPSHOT_UPSERT_SQL": "conjunctive",
    # A row's own scope column, compared scope-exactly. The global row
    # (a NULL scope) is selected by these and by nothing else.
    "pipelines/snapshot.py::KPI_TREND_SQL": "scope-exact",
    "pipelines/snapshot.py::COHORT_GRID_SIZE_SQL": "scope-exact",
    "pipelines/snapshot.py::COHORT_TREND_SQL": "scope-exact",
    "pipelines/snapshot.py::LATEST_KPI_DATE_SQL": "scope-exact",
    "pipelines/snapshot.py::LATEST_COHORT_DATE_SQL": "scope-exact",
    "pipelines/schedule.py::FETCH_RUN_SQL": "scope-exact",
    "pipelines/schedule.py::VALIDATED_JOBS_SQL": "scope-exact",
    "pipelines/schedule.py::_resolve_segment_run_id": "scope-exact",
    "pipelines/upload.py::FIND_JOB_SQL": "scope-exact",
    # --- the canonical shared text (db/queries/**) --------------------
    # The comparison views: the order side honours the brand's group
    # (through the groups join) and the customer side honours the
    # customer's group -- one scope parameter ($10 / $4).
    "db/queries/analytics_comparisons.sql::overview_comparison": "conjunctive",
    "db/queries/analytics_comparisons.sql::monthly_series": "conjunctive",
    # The brand-row family: the brand's group alone. The window bounds
    # and the store breakdown are about a brand's rows.
    "db/queries/analytics_comparisons.sql::window_bounds": "brand-row",
    "db/queries/analytics_comparisons.sql::store_windows": "brand-row",
    # The customer's group alone, and no brand side: a customer carries
    # no brand, so this query has nothing else to narrow by (the
    # statement says so in its own comment).
    "db/queries/analytics_comparisons.sql::customer_cohort_windows": "customer-only",
    # A row's own scope column, compared scope-exactly.
    "db/queries/connectors.sql::connector_sync_runs": "scope-exact",
    "db/queries/connectors.sql::connector_last_run": "scope-exact",
    # The customer's list: the customer's group ($6) and the brand named by
    # id ($7) -- a population filter on the customers table.
    "db/queries/customers.sql::list_customers": "customer-named",
    "db/queries/customers.sql::count_customers": "customer-named",
    # The score pages: the SNAPSHOT's own scope column (the scores are
    # stored per scope), so the global scope is selected too.
    "db/queries/customers.sql::page_clv_scores": "scope-exact",
    "db/queries/customers.sql::page_churn_scores": "scope-exact",
    "db/queries/kpi_daily.sql::upsert_kpi_daily": "scope-exact",
    "db/queries/kpi_daily.sql::kpi_pct_change": "scope-exact",
    "db/queries/kpi_daily.sql::clv_snapshots_list": "scope-exact",
    "db/queries/kpi_daily.sql::churn_snapshots_list": "scope-exact",
}


def _strip_comments(sql: str) -> str:
    return COMMENT_RE.sub(" ", sql)


def _alias_tables(sql: str) -> dict[str, str]:
    """alias -> table, for the FROM/JOIN clauses of one statement.

    A CTE's name is not a table: `FROM scoped_cust c` reads the CTE
    `scoped_cust`, and it must not shadow the `customers c` of the CTE
    that defined it -- the customer conjunct would then look like a
    filter on an unknown table.
    """
    ctes = {name.lower() for name in CTE_NAME_RE.findall(sql)}
    out: dict[str, str] = {}
    for table, alias in FROM_JOIN_RE.findall(sql):
        if not alias or alias.upper() in (
            "ON", "WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "SELECT",
            "AND", "OR", "SET", "VALUES", "AS", "LEFT", "INNER", "CROSS",
            "JOIN", "USING", "RETURNING", "INSERT", "UPDATE", "DELETE",
        ):
            continue
        if table.lower() in ctes:
            continue
        out.setdefault(alias.lower(), table.lower())
    return out


def _groups_source(sql: str) -> dict[str, str]:
    """alias of a `groups` join -> the column it reads the group from.

    `groups g ON g.id = b.group_id` reaches the BRAND's group;
    `groups g ON g.id = c.group_id` reaches the CUSTOMER's group.
    """
    out: dict[str, str] = {}
    for alias, source in GROUPS_JOIN_RE.findall(sql):
        out[alias.lower()] = source.lower()
    return out


def _own_table(sql: str) -> str | None:
    m = INSERT_INTO_RE.search(sql)
    return m.group(1).lower() if m else None


def _statements(sql: str) -> list[str]:
    """The statements of one literal or one shared file.

    A `;` inside a comment or a quoted string does NOT end a statement.
    The shared text carries prose in its comment blocks, and a sentence
    with a semicolon in it used to split the statement that followed it --
    so the inventory (and every key derived from it) depended on the
    punctuation of a comment.
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            buf.append(sql[i:j])
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        if sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql.startswith("''", j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue
        if sql[i] == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(sql[i])
        i += 1
    out.append("".join(buf))
    return [s for s in out if s.strip()]


def _main_table(sql: str) -> str | None:
    """The table a statement reads FROM -- the first that is not a CTE."""
    ctes = {name.lower() for name in CTE_NAME_RE.findall(sql)}
    for table, _alias in FROM_JOIN_RE.findall(sql):
        if table.lower() in ctes:
            continue
        return table.lower()
    return None


def _python_literals(path: pathlib.Path) -> list[tuple[str, str]]:
    """(label, sql) for every string literal in a module.

    A module-level constant is labelled `module::NAME`; a literal inside
    a function is labelled `module::function`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ROOT).as_posix()
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name):
                        out.append((f"{rel}::{target.id}", value.value))
            elif isinstance(value, ast.JoinedStr):
                # An f-string: its literal parts are what a reader sees.
                text = "".join(
                    part.value for part in value.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        out.append((f"{rel}::{target.id}", text))
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for func in funcs:
        for node in ast.walk(func):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append((f"{rel}::{func.name}", node.value))
    return out


def _conjuncts(sql: str) -> list[dict]:
    """The group comparisons of one statement, with their side."""
    sql = _strip_comments(sql)
    tables = _alias_tables(sql)
    groups_src = _groups_source(sql)
    own = _own_table(sql)
    found: list[dict] = []
    for m in GROUP_CONJUNCT_RE.finditer(sql):
        col, op, param = m.group("col"), " ".join(m.group("op").split()), m.group("param")
        alias = col.split(".")[0].lower() if "." in col else None
        side = "scope"
        if alias is None:
            # An unqualified group column is the column of the statement's
            # own table. Compared with `=` it is a POPULATION filter (which
            # members of that group are in scope); compared with
            # IS NOT DISTINCT FROM it is the row's OWN scope column (the
            # scope-shaped row, the global one included). A `=` on any other
            # table stays a scope column, so a scope-shaped row's filter
            # loosened to `=` still fails.
            if op == "=":
                side = {"customers": "customer", "brands": "brand"}.get(
                    _main_table(sql) or "", "scope")
        if alias is not None:
            table = tables.get(alias)
            if table == "brands":
                side = "brand"
            elif table == "customers":
                side = "customer"
            elif table == "groups":
                source = groups_src.get(alias)
                side = "brand" if source == "b" else (
                    "customer" if source == "c" else "scope")
            elif table == own:
                side = "scope"
            else:
                side = "scope"
        found.append({"col": col, "op": op, "param": param, "side": side,
                      "text": m.group(0)})
    for m in GROUPS_ID_RE.finditer(sql):
        col, op, param = m.group("col"), " ".join(m.group("op").split()), m.group("param")
        alias = col.split(".")[0].lower() if "." in col else None
        if alias is None or alias not in groups_src:
            continue
        source = groups_src[alias]
        side = "brand" if source == "b" else (
            "customer" if source == "c" else "scope")
        found.append({"col": col, "op": op, "param": param, "side": side,
                      "text": m.group(0)})
    return found


def _brand_named(sql: str) -> bool:
    """Does the statement name the brand (b.id = $n / s.brand_id = $n)?"""
    sql = _strip_comments(sql)
    if BRAND_ID_RE.search(sql):
        return True
    tables = _alias_tables(sql)
    for alias, table in tables.items():
        if table != "brands":
            continue
        if re.search(rf"\b{re.escape(alias)}\s*\.\s*id\s*=", sql, re.IGNORECASE):
            return True
    return False


def _collect() -> dict[str, list[dict]]:
    """label -> the group conjuncts of each of its statements."""
    out: dict[str, list[dict]] = {}
    for path in sorted(PIPELINES.rglob("*.py")):
        for label, text in _python_literals(path):
            if "group_id" not in text and "groups" not in text:
                continue
            rows: list[dict] = []
            for stmt in _statements(text):
                conj = _conjuncts(stmt)
                if conj:
                    rows.append({"sql": stmt, "conjuncts": conj})
            if rows:
                out.setdefault(label, []).extend(rows)
    for path in sorted(QUERIES.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "group_id" not in text:
            continue
        rel = path.relative_to(ROOT).as_posix()
        rows = []
        for index, stmt in enumerate(_statements(text), start=1):
            conj = _conjuncts(stmt)
            if not conj:
                continue
            named = STATEMENT_NAME_RE.search(stmt)
            key = (f"{rel}::{named.group(1)}" if named
                   else f"{rel}#{index}")
            rows.append({"sql": stmt, "conjuncts": conj, "key": key})
        if rows:
            out.setdefault(rel, []).extend(rows)
    return out


COLLECTED = _collect()
GROUP_FILTERING = {label: rows for label, rows in COLLECTED.items()}


def _keys() -> set[str]:
    """The inventory keys of every group-filtering statement."""
    keys: set[str] = set()
    for label, rows in GROUP_FILTERING.items():
        for row in rows:
            keys.add(row.get("key", label))
    return keys


def test_every_group_filtering_statement_is_classified():
    """The pin is exhaustive: an unclassified group filter fails."""
    unclassified = sorted(_keys() - set(FAMILIES))
    assert not unclassified, (
        "a statement filters by group and is not in FAMILIES: "
        + "; ".join(unclassified)
    )


def test_no_removed_statement_is_still_classified():
    """A classified statement that no longer filters by group fails."""
    stale = sorted(set(FAMILIES) - _keys())
    assert not stale, (
        "FAMILIES lists a statement that no longer filters by group: "
        + "; ".join(stale)
    )


def _derived_family(row: dict) -> str:
    """The family the predicates describe.

    A population conjunct is one that reads the CUSTOMER's group or the
    BRAND's group; a conjunct on a row's own scope column is not a
    population (it selects the scope-shaped row itself), so it does not
    take part in the family.
    """
    sides = {conj["side"] for conj in row["conjuncts"]}
    population = sides & {"brand", "customer"}
    if not population:
        return "scope-exact"
    if population == {"brand", "customer"}:
        return "conjunctive"
    if "customer" in population:
        return "customer-named" if _brand_named(row["sql"]) else "customer-only"
    return "brand-row"


@pytest.mark.parametrize("label", sorted(GROUP_FILTERING))
def test_each_group_filter_has_a_legal_form(label):
    """Every group comparison is a population `=` or a scope-exact one."""
    for row in GROUP_FILTERING[label]:
        for conj in row["conjuncts"]:
            if conj["side"] == "scope":
                assert conj["op"] == "IS NOT DISTINCT FROM", (
                    f"{label}: a row's own scope column must be compared with "
                    f"IS NOT DISTINCT FROM (a bare {conj['op']!r} drops the "
                    f"global scope): {conj['text']!r}"
                )
            else:
                assert conj["op"] == "=", (
                    f"{label}: a population filter compares a group column "
                    f"with '=': {conj['text']!r}"
                )


@pytest.mark.parametrize("label", sorted(GROUP_FILTERING))
def test_one_scope_parameter_per_statement(label):
    """No statement compares a group column against two parameters."""
    for row in GROUP_FILTERING[label]:
        params = {conj["param"] for conj in row["conjuncts"]}
        assert len(params) <= 1, (
            f"{label}: the group is compared against {sorted(params)} -- one "
            f"statement, one scope parameter"
        )


@pytest.mark.parametrize("label", sorted(GROUP_FILTERING))
def test_the_family_matches_the_predicates(label):
    """The declared family is the one the predicates describe."""
    for row in GROUP_FILTERING[label]:
        key = row.get("key", label)
        declared = FAMILIES[key]
        derived = _derived_family(row)
        assert declared == derived, (
            f"{key}: declared {declared!r}, the predicates describe "
            f"{derived!r}"
        )


def test_the_families_are_reported():
    """The populations that exist today, counted -- the price note."""
    counts: dict[str, int] = {}
    for label, rows in GROUP_FILTERING.items():
        for row in rows:
            key = row.get("key", label)
            family = FAMILIES.get(key)
            if family:
                counts[family] = counts.get(family, 0) + 1
    assert counts, "no group filter was found -- the scan is broken"
    print("\ngroup-filter families:", counts)
