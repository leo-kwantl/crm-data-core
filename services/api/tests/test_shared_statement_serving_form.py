"""The shared file carries the SERVING form (manager ruling, 2026-10-22).

A `db/queries/<resource>.sql` statement is what the runtime serves, and a
runtime that inlines a copy of a named statement is token-equal to it. The
fork this check closes is the one a cross-brand leak lived in: the file kept
the unscoped form while the runtime's copy carried the scope, so the two
answered different questions about the same statement.

The pairing is by the statement's name: `ALIASES` names the runtime
counterpart of every shared statement - a module-level constant, or the
function that sends it. A statement with no counterpart found is a FAILURE,
not a skip: the pairing is the check, and a check that quietly stops
checking is the fork coming back.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
QUERIES = ROOT / "db" / "queries"
# The runtime is the whole API tree (minus data's own test harness).
RUNTIME_TREE = ROOT / "services" / "api"

# statement name -> (kind, name) of the runtime counterpart: a module-level
# constant ("const"), or the function whose conn.fetch/execute argument is
# the statement ("func"). A function may send several statements; the
# statement's text must be one of them.
ALIASES = {
    # a runtime constant
    "list_inventory_for_product": ("const", "LIST_INVENTORY_SQL"),
    "list_customer_orders": ("const", "LIST_ORDERS_SQL"),
    "get_order_header": ("const", "ORDER_HEADER_SQL"),
    "list_order_items": ("const", "ORDER_ITEMS_SQL"),
    # a statement the runtime inlines in a function
    "customer_by_id": ("func", "_build_profile"),
    "customer_consents": ("func", "_build_profile"),
    "customer_contacts": ("func", "_build_profile"),
    "customer_purchase_summary": ("func", "_build_profile"),
    "customer_insert": ("func", "create_customer"),
    "customer_contact_insert": ("func", "create_customer"),
    "customer_update_name": ("func", "update_customer"),
    "customer_merge_attributes": ("func", "update_customer"),
    "customer_replace_contacts_delete": ("func", "update_customer"),
    "customer_soft_delete": ("func", "soft_delete_customer"),
    "staff_insert": ("func", "create_staff"),
    "staff_by_id": ("func", "get_staff"),
    "staff_list_page": ("func", "list_staff_page"),
    "staff_linked_user_count": ("func", "delete_staff"),
    "staff_delete": ("func", "delete_staff"),
    "user_insert": ("func", "create_user"),
    "user_by_id": ("func", "get_user"),
    "user_list_page": ("func", "list_users_page"),
    "unlinked_staff": ("func", "list_unlinked_staff"),
}

# The statements the runtime does NOT serve: data's own read path for the
# per-customer CLV / churn scores (data's `test_customer_scores_read.py`
# runs them against the database). They have no runtime copy, so there is
# nothing to be token-equal to - and the exemption is pinned by a test.
EXEMPT = {"page_clv_scores", "page_churn_scores"}

# The shared files whose statements the runtime serves.
SHARED_FILES = ("orders.sql", "customers.sql", "products.sql", "identity.sql")

# The runtime calls that carry a statement.
SENDERS = {"fetch", "fetchrow", "fetchval", "execute"}


def _normalise(sql: str) -> str:
    """Token-equal means the same SQL: no comments, no whitespace, case-folded."""
    body = "\n".join(
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    )
    body = body.replace(";", " ")
    return re.sub(r"\s+", " ", body).strip().lower()


def _sql_head(text: str) -> str:
    """The first word of the SQL, ignoring the leading comment lines.

    A runtime copy of a named statement carries the file's `-- name:`
    marker and its params comments, so the first line is not the SQL any
    more -- and a runtime that filters its statements by "does it start
    with SELECT" would see none of them.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.split(None, 1)[0].upper()
    return ""


def _file_statements(files: tuple[str, ...] | None = None) -> dict[str, str]:
    """The named statements of every shared file: name -> SQL body."""
    out: dict[str, str] = {}
    for path in sorted(QUERIES.glob("*.sql")):
        if files is not None and path.name not in files:
            continue
        text = path.read_text(encoding="utf-8")
        current: str | None = None
        lines: list[str] = []
        for line in text.splitlines():
            marker = re.match(r"--\s*name:\s*(\S+)\s*$", line)
            if marker:
                if current:
                    out[current] = "\n".join(lines)
                current = marker.group(1)
                lines = []
                continue
            if current is not None:
                if line.strip().startswith("--"):
                    continue
                lines.append(line)
                if line.strip().endswith(";"):
                    out[current] = "\n".join(lines)
                    current = None
                    lines = []
        if current:
            out[current] = "\n".join(lines)
    return out


def _runtime_files() -> list[pathlib.Path]:
    """Every runtime module under services/api, minus data's own tests."""
    return [
        path for path in sorted(RUNTIME_TREE.rglob("*.py"))
        if "tests" not in path.relative_to(RUNTIME_TREE).parts
    ]


def _fold(node, constants: dict[str, str]) -> str | None:
    """The string value of a node built only from string constants.

    A runtime statement is often assembled (`_USER_SELECT + " WHERE ..."`),
    so the text is folded here instead of read out of the source: a
    statement built from three literals is one string, not three.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold(node.left, constants)
        right = _fold(node.right, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None
            parts.append(value.value)
        return "".join(parts)
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """The module-level string constants, folded where they are assembled."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = _fold(node.value, constants) if node.value is not None else None
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _runtime_constants() -> dict[str, list[str]]:
    """Every module-level SQL string in the mirrored runtime: name -> texts."""
    out: dict[str, list[str]] = {}
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                value = constants.get(target.id)
                if not value:
                    continue
                head = _sql_head(value)
                if head not in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}:
                    continue
                out.setdefault(target.id, []).append(value)
    return out


def _function_statements(constants: dict[str, list[str]], func_name: str) -> list[str]:
    """The SQL statements a runtime function sends, assembled from its parts."""
    found: list[str] = []
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_constants(tree)
        flat = {name: texts[0] for name, texts in constants.items()}
        flat.update(module)
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name != func_name:
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in SENDERS
                    and call.args
                ):
                    text = _fold(call.args[0], flat)
                    if text:
                        found.append(text)
    return found


def _runtime_sql_strings() -> set[str]:
    """Every SQL string the runtime can send, normalised."""
    out: set[str] = set()
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, (ast.BinOp, ast.JoinedStr)):
                text = _fold(node, constants)
                if text is None:
                    continue
            else:
                continue
            head = _sql_head(text)
            if head in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}:
                out.add(_normalise(text))
    return out


def _pairs() -> list[tuple[str, str, str, list[str] | None]]:
    """(statement name, counterpart name, file body, counterpart texts)."""
    statements = _file_statements(SHARED_FILES)
    constants = _runtime_constants()
    pairs: list[tuple[str, str, str, list[str] | None]] = []
    for name, body in sorted(statements.items()):
        if name in EXEMPT or not _normalise(body):
            continue
        kind, target = ALIASES.get(name, ("const", name.upper() + "_SQL"))
        if kind == "const":
            texts = constants.get(target)
        else:
            found = _function_statements(constants, target)
            texts = found or None
        pairs.append((name, target, body, texts))
    return pairs


PAIRS = _pairs()


def test_the_pairs_are_found():
    """The scan finds the forks -- without it the check would pass on nothing."""
    assert len(PAIRS) >= 30, f"only {len(PAIRS)} shared statements paired"


def test_every_named_statement_has_a_counterpart():
    """A statement whose runtime counterpart is missing is a FAILURE.

    A pairing that quietly stops pairing is the fork coming back: the file
    would drift from the runtime with nothing to say so.
    """
    missing = [
        f"  {name}: the runtime counterpart {target} was not found"
        for name, target, _body, texts in PAIRS
        if texts is None
    ]
    assert not missing, (
        "a shared statement has no runtime counterpart:\n" + "\n".join(missing)
    )


def test_each_shared_statement_is_the_serving_form():
    """The file's statement and the runtime's copy are token-equal."""
    forked = [
        f"  {name}: the file and the runtime's {target} differ\n"
        f"    file:    {_normalise(file_sql)}\n"
        f"    runtime: {sorted(_normalise(t) for t in texts)}"
        for name, target, file_sql, texts in PAIRS
        if texts is not None
        and _normalise(file_sql) not in {_normalise(t) for t in texts}
    ]
    assert not forked, (
        "a shared statement is not the serving form -- the shared file must "
        "carry the runtime's form:\n" + "\n".join(forked)
    )


def test_every_named_statement_is_served_verbatim():
    """Every named statement of the shared files is what the runtime sends.

    The named pairs above are the ones a reader can name; this is the
    whole set, so a fold cannot be undone by editing a constant's name.
    """
    statements = _file_statements(SHARED_FILES)
    served = _runtime_sql_strings()
    unserved = []
    for name, body in sorted(statements.items()):
        if name in EXEMPT or not _normalise(body):
            continue
        if _normalise(body) not in served:
            unserved.append(f"  {name}: the file's text is not served verbatim")
    assert not unserved, (
        "a shared statement is not served verbatim -- the shared file must "
        "carry the runtime's text:\n" + "\n".join(unserved)
    )


def test_the_exemptions_are_the_ones_we_say():
    """The exemption list cannot grow quietly."""
    assert EXEMPT == {"page_clv_scores", "page_churn_scores"}, EXEMPT
    named = {
        name
        for name, body in _file_statements(SHARED_FILES).items()
        if name and _normalise(body)
    }
    assert len(named) - len(EXEMPT) >= 30, f"only {len(named)} named statements"
