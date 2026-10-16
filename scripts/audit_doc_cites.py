"""Audit ``path.ext:LINE`` citations in markdown against the trees they name.

Why this exists (2026-10-17): the cheap "is the cite out of range" guard has a
false-positive mode that is *mirror drift, not documentation*. ``pipelines/
contracts.md`` cites ``scope.py:72-76`` *"at app 509c56e"* — exactly right in
app's tree, while data's mirrored copy of that same path is 26 lines shorter, so
a resolver that only looks in-repo reports a correct cross-tree citation as
stale. So a cite that resolves in *some* checked-out tree is reported as
``disagrees`` (info, not failure), and only a cite out of range in *every* tree
holding that path is a hard failure. The rule the guard encodes: resolve against
the tree the cite names; ``services/api/...`` and ``db/queries/...`` refer to
app's checkout, data's copies being CI/harness targets.

Read-only, stdlib only. Exit 1 only on a hard failure (out of range everywhere,
or — under ``--strict`` — a path in no tree).

    python scripts/audit_doc_cites.py
    python scripts/audit_doc_cites.py --strict
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
EXTS = ("py", "sql", "md", "ts", "tsx", "js", "jsx", "json", "yaml", "yml",
        "toml", "sh")
CITE = re.compile(r"[\w./-]+\.(?:" + "|".join(EXTS) + r"):(\d+)(?:\s*-\s*(\d+))?")


def iter_md(root: Path):
    for p in sorted(root.rglob("*.md")):
        if not any(part in SKIP_DIRS for part in p.parts):
            yield p


def line_count(p: Path) -> int:
    with p.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def basename_index(roots: list[Path]) -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = {}
    for root in roots:
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lstrip(".") not in EXTS:
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            idx.setdefault(p.name, []).append(p)
    return idx


def audit(root: Path, peers: list[Path], strict: bool) -> int:
    roots = [root, *peers]
    idx = basename_index(roots)
    counts = {"ok": 0, "disagrees": 0, "out-of-range": 0, "unresolved": 0}
    hard: list[str] = []
    info: list[str] = []

    for doc in iter_md(root):
        rel_doc = doc.relative_to(root)
        with doc.open(encoding="utf-8", errors="replace") as fh:
            for lineno, text in enumerate(fh, 1):
                for m in re.finditer(
                        r"[\w./-]+\.(?:" + "|".join(EXTS) + r"):\d+(?:\s*-\s*\d+)?", text):
                    span = m.group(0)
                    if text[max(0, m.start() - 3):m.start()] == "://":
                        continue  # a URL, not a citation
                    inner = CITE.match(span)
                    if not inner:
                        continue
                    cited = span.rsplit(":", 1)[0]
                    start, end = int(inner.group(1)), int(inner.group(2) or inner.group(1))
                    where: list[tuple[str, int, bool]] = []  # (tree, lines, in_range)
                    for r in roots:
                        cand = r / cited
                        if cand.is_file():
                            n = line_count(cand)
                            where.append((r.name, n, start <= n and end <= n))
                    if not where:
                        # A bare basename: evaluate every tree that holds it. A
                        # verdict split across trees is the mirror-drift signal
                        # (the cite is right where it was read, wrong against a
                        # shorter copy elsewhere), so it is reported, not failed.
                        for h in idx.get(Path(cited).name, []):
                            n = line_count(h)
                            tree = next((r.name for r in roots if r in h.parents), "?")
                            where.append((tree, n, start <= n and end <= n))
                    if not where:
                        counts["unresolved"] += 1
                        (hard if strict else info).append(
                            f"unresolved  {rel_doc}:{lineno} -> {span} "
                            f"(path in no tree)")
                    elif all(ok for _, _, ok in where):
                        counts["ok"] += 1
                    elif any(ok for _, _, ok in where):
                        counts["disagrees"] += 1
                        info.append(
                            "disagrees   %s:%d -> %s [%s]"
                            % (rel_doc, lineno, span,
                               ", ".join("%s=%dL %s" % (n, c, "ok" if ok else "OUT")
                                         for n, c, ok in where)))
                    else:
                        counts["out-of-range"] += 1
                        hard.append(
                            f"out-of-range %s:%d -> %s [%s]" % (
                                rel_doc, lineno, span,
                                ", ".join(f"{n}={c}L" for n, c, _ in where)))
            # end line loop
    print("trees: " + ", ".join(str(r) for r in roots if r.is_dir()))
    print("citations: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    for line in info + hard:
        print("  " + line)
    return 1 if hard else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit markdown code-line citations.")
    ap.add_argument("--repo", default=".", help="repo whose markdown to audit")
    ap.add_argument("--peer", nargs="*", default=None,
                    help="trees to resolve cites against (default: sibling "
                         "crm-cdp-app / crm-cdp-ml if present)")
    ap.add_argument("--strict", action="store_true",
                    help="also fail when a cited path is in no tree")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if args.peer is None:
        peers = [p for p in (root.parent / "crm-cdp-app", root.parent / "crm-cdp-ml")
                 if p.is_dir()]
    else:
        peers = [Path(p).resolve() for p in args.peer]
    return audit(root, peers, args.strict)


if __name__ == "__main__":
    sys.exit(main())
