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

It also checks **pins**. ``contracts.md`` states a per-file anchor as
`` `path` `sha` `` — "that file's last-changed revision" — and a line range
cannot see whether a cited revision is still the one in force. The pin check
is the other half: ``git log -1 --format=%h -- <path>`` must reproduce the
pinned sha in some checked-out tree. This exists because it fired the day the
convention was agreed (2026-10-17): ml handed over ``c61fa0d`` for
``ml_engine/store_ld8.py`` while that file's last change was already
``36e65e6`` three commits later, so the value was stale *as written*. **A pin is
regenerated, never copied** — it rots between the message that states it and
the doc that stores it, and a bare ``path`` ``sha`` pair is read as a claim
about the file's *current* last change, not as history. A historical anchor
("naming ``f889386``", "``path:LINE`` at ``sha``") is prose and does not match
the pin form.

Read-only, stdlib only. Exit 1 on a hard failure (a cite out of range in every
tree holding it, a stale pin; or — under ``--strict`` — a path in no tree).

    python scripts/audit_doc_cites.py
    python scripts/audit_doc_cites.py --strict
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
EXTS = ("py", "sql", "md", "ts", "tsx", "js", "jsx", "json", "yaml", "yml",
        "toml", "sh")
CITE = re.compile(r"[\w./-]+\.(?:" + "|".join(EXTS) + r"):(\d+)(?:\s*-\s*(\d+))?")
# `path` `sha`  (optionally `path` at `sha`) — a per-file anchor, see docstring.
PIN = re.compile(r"`([\w./-]+\.(?:" + "|".join(EXTS) + r"))`\s*(?:at\s+)?`([0-9a-f]{7,40})`")


def iter_md(root: Path):
    for p in sorted(root.rglob("*.md")):
        if not any(part in SKIP_DIRS for part in p.parts):
            yield p


def line_count(p: Path) -> int:
    with p.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def last_changed(tree: Path, rel: str) -> str | None:
    """Short sha of the commit that last touched ``rel`` in ``tree``."""
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "log", "-1", "--format=%h", "--", rel],
            capture_output=True, text=True, encoding="utf-8",  # console is cp950 here
            errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def audit_pins(root: Path, roots: list[Path],
               idx: dict[str, list[Path]]) -> tuple[dict[str, int], list[str], list[str]]:
    """Check `` `path` `sha` `` anchors against each tree's last-changed revision."""
    counts = {"pin-ok": 0, "pin-stale": 0, "pin-unresolved": 0}
    hard: list[str] = []
    info: list[str] = []
    seen: set[tuple[str, str]] = set()
    for doc in iter_md(root):
        rel_doc = doc.relative_to(root)
        with doc.open(encoding="utf-8", errors="replace") as fh:
            for lineno, text in enumerate(fh, 1):
                for m in PIN.finditer(text):
                    path, pinned = m.group(1), m.group(2)
                    if (path, pinned) in seen:
                        continue
                    seen.add((path, pinned))
                    found: list[tuple[str, str]] = []
                    for r in roots:
                        if (r / path).is_file():
                            got = last_changed(r, path)
                            if got:
                                found.append((r.name, got))
                    if not found:
                        # bare basename, as in the cite pass: a pin on
                        # `031_x.sql` resolves in whichever tree holds it.
                        for h in idx.get(Path(path).name, []):
                            tree = next((r for r in roots if r in h.parents), None)
                            if tree is None:
                                continue
                            got = last_changed(tree, str(h.relative_to(tree)).replace("\\", "/"))
                            if got:
                                found.append((tree.name, got))
                    if not found:
                        counts["pin-unresolved"] += 1
                        info.append(f"pin-unknown {rel_doc}:{lineno} -> {path} "
                                    f"(path in no tree)")
                    elif any(got.startswith(pinned) or pinned.startswith(got)
                             for _, got in found):
                        counts["pin-ok"] += 1
                    else:
                        counts["pin-stale"] += 1
                        hard.append(
                            "stale-pin   %s:%d -> %s pinned %s, last changed %s" % (
                                rel_doc, lineno, path, pinned,
                                ", ".join(f"{g} ({n})" for n, g in found)))
    return counts, hard, info


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
    pin_counts, pin_hard, pin_info = audit_pins(root, roots, idx)
    print("trees: " + ", ".join(str(r) for r in roots if r.is_dir()))
    print("citations: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("pins: " + ", ".join(f"{k}={v}" for k, v in pin_counts.items()))
    for line in info + pin_info + hard + pin_hard:
        print("  " + line)
    return 1 if (hard or pin_hard) else 0


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
