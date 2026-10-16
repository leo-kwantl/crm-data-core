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
    python scripts/audit_doc_cites.py --comments
    python scripts/audit_doc_cites.py --strict

``--comments`` extends the same genres to the places the cites actually live:
comment lines (``#`` in .py, ``--`` in .sql) and triple-quoted string bodies,
where ml's and app's anchors are. Two boundaries are stated in the output rather
than left to the reader: a default run is a pass over **markdown only**, and a
trailing ``# …`` after code is not read, because attributing it means guessing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
EXTS = ("py", "sql", "md", "ts", "tsx", "js", "jsx", "json", "yaml", "yml",
        "toml", "sh")
# What ``--comments`` reads out of a code file. A sha-shaped token in code is not
# a citation, so only comment lines and triple-quoted bodies are scanned.
COMMENT_PREFIX = {".py": ("#",), ".sql": ("--",)}
CITE = re.compile(r"[\w./-]+\.(?:" + "|".join(EXTS) + r"):(\d+)(?:\s*-\s*(\d+))?")
# Two genres of anchor, and **the form decides which** (app+ml, 2026-10-17) — a
# guard that infers the genre by asking "does this sha equal `git log -1`?"
# *decides* it instead, and the "fix" for a historical label would then be to
# rewrite it to HEAD, deleting the provenance it exists to carry:
#   `path` `sha`    (nothing between) -> PIN: the file's last-changed revision.
#   `path` ... `sha` (a word between) -> REVISION LABEL: dated history, checked
#                                       for existence only, never rewritten.
# A chain `a.py`/`b.py` `sha` is still a PIN, verified per path, so a shared sha
# cannot hide behind a continuation.
SHA = r"[0-9a-f]{7,40}"
PATH_TOK = r"`[\w./-]+\.(?:" + "|".join(EXTS) + r")`"
# The cue sits between the **last** path token and the sha; a repo name in front
# of the path attributes the path's owner, not the revision, so a cross-repo
# anchor keeps the genre its own form declares -- `ml `x.py` `sha`` is a pin,
# `ml `x.py` as of `sha`` a label (app, 2026-10-17).
#
# The values here are schematic on purpose. This comment carried the same example
# with real values (ml's `store_ld8.py` and a real revision) until the comment
# pass flagged it as a stale pin within the hour: **an example written with real
# values IS a citation**, so the fix is to stop writing live numbers into prose
# about the rule, not to keep bumping them.
PIN = re.compile(r"((?:" + PATH_TOK + r"(?:\s*[/,]\s*)?)+)\s*`(" + SHA + r")`")
PIN_PATH = re.compile(r"`([\w./-]+\.(?:" + "|".join(EXTS) + r"))`")
SHA_TOK = re.compile(r"`(" + SHA + r")`")


def repo_files(root: Path) -> list[Path] | None:
    """Tracked + untracked-not-ignored files; ``None`` when git cannot say.

    Walking the filesystem instead audits example placeholders inside ignored
trees (``.pi/`` vendored skill docs, app's find 2026-10-17): two permanent
findings nobody can fix, which is how ``unclassified=0`` stops being
achievable and a guard stops being read.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [root / name for name in out.stdout.split("\0") if name]


def iter_md(root: Path, comments: bool = False):
    """Markdown files, plus the comment-bearing code files under ``--comments``."""
    suffixes = {".md"} | ({".py", ".sql"} if comments else set())
    files = repo_files(root)
    if files is None:  # not a git checkout: fall back to a directory walk
        files = [p for p in root.rglob("*")
                 if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)]
    for p in sorted(files):
        if p.suffix in suffixes:
            yield p


def text_units(doc: Path, comments: bool) -> list[tuple[int, str]]:
    """The ``(lineno, text)`` units the guard reads for one file.

    Markdown is read whole. Under ``--comments`` a code file contributes only its
comment lines and triple-quoted string bodies -- the surface where the cites
actually are -- with real line numbers, so a finding is still ``path:LINE``.
    """
    lines = list(enumerate(doc.read_text(encoding="utf-8", errors="replace")
                           .splitlines(keepends=True), 1))
    if not comments or doc.suffix == ".md":
        return lines
    prefixes = COMMENT_PREFIX.get(doc.suffix, ("#",))
    units: list[tuple[int, str]] = []
    quote: str | None = None
    for lineno, raw in lines:
        stripped = raw.strip()
        if quote is not None:
            units.append((lineno, raw))
            if stripped.count(quote) % 2 == 1:
                quote = None
            continue
        opener = next((q for q in ('"""', "'''") if stripped.startswith(q)), None)
        if opener is not None:
            units.append((lineno, raw))
            if stripped.count(opener) % 2 == 1:
                quote = opener
            continue
        if any(stripped.startswith(pre) for pre in prefixes):
            units.append((lineno, raw))
    return units


@contextmanager
def doc_units(doc: Path, comments: bool):
    """Context-shaped seam so the audit loop reads units instead of a handle."""
    yield text_units(doc, comments)


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


def commit_exists(tree: Path, sha: str) -> bool:
    """True when ``sha`` names a commit in ``tree`` (a label may be cross-repo)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "cat-file", "-e", sha + "^{commit}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


# Repo attribution for a sha with no path: the nearest repo name in the same
# sentence. Deliberately a window rather than an adjacency test, because prose
# carries words between ("ml handed it over as `<sha>`"); a wrong guess is not
# silent -- the sha then fails to exist in the claimed repo and is reported with
# the repos that do hold it.
REPO_TOK = re.compile(r"(?:crm-cdp-(app|data|ml)|\b(app|data|ml)\b)", re.I)
# What says an 8-hex token is not a revision at all: row/pair/id context.
ROWISH = re.compile(r"\b(row|rows|pair|id|snapshot|window_key|batch)\b", re.I)
CITE_RANGE = re.compile(r"([\w./-]+\.(?:py|sql|ts|tsx|md|yaml|json|sh)):(\d+)(?:\s*-\s*(\d+))?")


def path_at_rev(tree: Path, sha: str, path: str) -> bool:
    """True when ``path`` existed at ``sha`` -- the check a label's path buys."""
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "cat-file", "-e", "%s:%s" % (sha, path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def reachable_refs(tree: Path, sha: str) -> list[str]:
    """Advertised refs (``origin/*``, tags) reaching ``sha``; empty = unreferenced.

    Judged against *advertised* refs rather than ancestry-of-HEAD or local
    branches, because a citation asks "will a reader who clones this repo resolve
    this sha?": a never-pushed branch, or an ``origin/<branch>`` left behind
    after the branch was deleted upstream, keeps the object alive here and
    absent in a fresh clone (app, 2026-10-17).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "for-each-ref", "--contains", sha,
             "--format=%(refname)", "refs/remotes/origin/*", "refs/tags/*"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return [x.strip() for x in out.stdout.splitlines() if x.strip()]


def changed_spans(tree: Path, old: str, new: str, path: str) -> list[tuple[int, int]]:
    """New-file line ranges the path gained or lost between two revisions.

    This is what separates a pin that went stale because a peer edited prose in
    the file from one whose cited lines actually moved.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "diff", "--unified=0", old, new, "--", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    spans: list[tuple[int, int]] = []
    for line in out.stdout.splitlines():
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
        if not m:
            continue
        start, count = int(m.group(1)), int(m.group(2) or 1)
        spans.append((start, start + max(count, 1) - 1) if count
                     else (start, start))
    return spans


# The only cue words that make a revision label. Deliberately a closed list
# (app, 2026-10-17): inference — "does this sha look historical?" — is the thing
# being removed, and "misclassify a pin as a label" means its staleness is never
# checked again. A sha that declares neither genre is reported as
# `unclassified` and left unjudged, so the silence cannot move to a third
# bucket: `unclassified=0` reads "every sha in this doc declared its genre".
LABEL_CUE = re.compile(r"\b(read|measured|as)\s+(?:at|of)\b", re.I)


def owner_trees(path: str, roots: list[Path],
                idx: dict[str, list[Path]]) -> list[tuple[Path, str]]:
    """Trees holding ``path``, with its repo-relative form in each.

    Bare basenames resolve through ``idx`` (as in the cite pass), because a pin
    on `031_x.sql` is meaningful in whichever tree holds that file.
    """
    out: list[tuple[Path, str]] = []
    for r in roots:
        if (r / path).is_file() and last_changed(r, path):
            out.append((r, path))
    if not out:
        for h in idx.get(Path(path).name, []):
            tree = next((r for r in roots if r in h.parents), None)
            if tree is None:
                continue
            rel = str(h.relative_to(tree)).replace("\\", "/")
            if last_changed(tree, rel):
                out.append((tree, rel))
    return out


def _check_pin(counts, hard, info, seen, rel_doc, lineno, path, pinned, roots, idx,
               cites):
    """A pin asserts the file's *current* last-changed revision."""
    if (path, pinned) in seen:
        return
    seen.add((path, pinned))
    found = [(r, rel, last_changed(r, rel)) for r, rel in owner_trees(path, roots, idx)]
    found = [(r, rel, got) for r, rel, got in found if got]
    if not found:
        counts["pin-unresolved"] += 1
        info.append(f"pin-unknown {rel_doc}:{lineno} -> {path} (path in no tree)")
    elif any(got.startswith(pinned) or pinned.startswith(got) for _, _, got in found):
        counts["pin-ok"] += 1
    else:
        counts["pin-stale"] += 1
        tree, rel, got = found[0]
        # A stale pin is not automatically a moved cite: a peer editing prose in
        # the same file (ml declaring genres inside a comment, say) trips the
        # equality check without touching anything that was cited.
        spans = changed_spans(tree, pinned, got, rel)
        collided = [c for c in cites if any(a <= c[1] <= b or a <= c[2] <= b
                                            for a, b in spans)]
        if not cites:
            note = "no cite of this path here"
        elif collided:
            note = "CITED LINES CHANGED at " + ", ".join(
                ":%d%s" % (c[1], "" if c[2] == c[1] else "-%d" % c[2])
                for c in collided)
        else:
            note = "cited lines untouched (%d cite(s) of this path)" % len(cites)
        hard.append(
            "stale-pin   %s:%d -> %s pinned %s, last changed %s [%s]" % (
                rel_doc, lineno, path, pinned,
                ", ".join(f"{g} ({r.name})" for r, _, g in found), note))


def audit_pins(root: Path, roots: list[Path], idx: dict[str, list[Path]],
               comments: bool = False) -> tuple[dict[str, int], list[str], list[str]]:
    """Check both anchor genres: pins for equality, revision labels for existence."""
    counts = {"pin-ok": 0, "pin-stale": 0, "pin-unresolved": 0,
              "label-ok": 0, "label-unreferenced": 0, "label-bad": 0,
              "label-unknown": 0, "label-cross": 0, "repo-ok": 0,
              "repo-unreferenced": 0,
              "repo-elsewhere": 0, "no-commit": 0, "unclassified": 0,
              "sentinel-skipped": 0, "rowid-skipped": 0}
    hard: list[str] = []
    info: list[str] = []
    seen: set[tuple[str, str]] = set()
    for doc in iter_md(root, comments):
        rel_doc = doc.relative_to(root)
        lines = text_units(doc, comments)
        # every `path:LINE[-LINE]` this doc makes, keyed by path: a stale pin is
        # judged by whether one of these ranges actually moved.
        cites_by_path: dict[str, list[tuple[int, int, int]]] = {}
        for ln, t in lines:
            for cm in CITE_RANGE.finditer(t):
                cites_by_path.setdefault(cm.group(1), []).append(
                    (ln, int(cm.group(2)), int(cm.group(3) or cm.group(2))))
        for i, (lineno, text) in enumerate(lines):  # `idx` is the basename index
                # Repo attribution may sit a line earlier than the sha it
                # describes ("ml handed it over as" / "`<sha>`"), so the
                # lookback spans a few lines and the offset keeps indices exact.
                ctx = "\n".join(t for _, t in lines[max(0, i - 5):i + 1])
                base = len(ctx) - len(text)
                # Prose is wrapped, so an anchor can straddle the wrap
                # (`<path>` on one line, its `sha` on the next). Analysis runs on
                # the window and reports only what resolves on this line, which
                # `seen` then keeps from being counted twice.
                pinned_spans: list[tuple[int, int]] = []
                for m in PIN.finditer(ctx):
                    if m.end() <= base:
                        continue
                    pinned_spans.append(m.span())
                    paths = re.findall(PIN_PATH, m.group(1))
                    sha = m.group(2)
                    for path in paths:
                        _check_pin(counts, hard, info, seen, rel_doc, lineno,
                                   path, sha, roots, idx,
                                   cites_by_path.get(path, []))
                for m in SHA_TOK.finditer(ctx):
                    if m.start() < base:
                        continue
                    if any(a <= m.start() and m.end() <= b for a, b in pinned_spans):
                        continue  # adjacency already made it a pin
                    sha = m.group(1)
                    if set(sha) <= {"0"}:
                        # The SQL no-scope sentinel `COALESCE(brand_id, '000…'::uuid)`
                        # means "no scope was recorded" -- a convention, not a
                        # citation, and distinct from ml's required-placeholder
                        # (`ml_jobs/pipelines.example.yaml:57-59`), where the
                        # runner fills the key in.
                        counts["sentinel-skipped"] += 1
                        continue
                    paths = list(PIN_PATH.finditer(ctx[:m.start()]))
                    if paths and ctx[paths[-1].end():m.start()].count("\n") > 1:
                        # The window spans six lines so a wrapped anchor still
                        # resolves; a path a paragraph away is somebody else's.
                        paths = []
                    cue = ctx[paths[-1].end():m.start()] if paths else ""
                    if paths and LABEL_CUE.search(cue):
                        # A path-anchored label buys one check a repo-anchored
                        # one cannot: the path has to have existed *at* that sha.
                        owns = owner_trees(paths[-1].group(1), roots, idx)
                        if not owns:
                            counts["label-unknown"] += 1
                            info.append(
                                "label-unknown %s:%d -> %s `%s` (path in no tree, so"
                                " the pair cannot be checked)" % (
                                    rel_doc, lineno, paths[-1].group(1), sha))
                        elif any(path_at_rev(t, sha, rel) for t, rel in owns):
                            counts["label-ok"] += 1
                            if not any(reachable_refs(t, sha) for t, _ in owns):
                                counts["label-unreferenced"] += 1
                                info.append(
                                    "label-unreferenced %s:%d -> `%s` existed with"
                                    " %s but no origin ref or tag reaches it (a"
                                    " fresh clone cannot resolve it; dies at the"
                                    " next gc)" % (rel_doc, lineno, sha,
                                                   paths[-1].group(1)))
                        elif [r for r in roots if r is not owns[0][0]
                              and commit_exists(r, sha)]:
                            # The sha is a commit in another repo while the path
                            # lives in this one: a legitimate cross-repo label,
                            # which this pair cannot check either way.
                            counts["label-cross"] += 1
                            info.append(
                                "label-cross %s:%d -> `%s` is a commit elsewhere;"
                                " %s is not a path in that repo (cross-repo reference,"
                                " uncheckable as a pair)"
                                % (rel_doc, lineno, sha, paths[-1].group(1)))
                        else:
                            counts["label-bad"] += 1
                            info.append(
                                "label-bad   %s:%d -> `%s` did not exist at `%s` "
                                "(existence + ancestry are all this proves, never"
                                " content)" % (rel_doc, lineno, paths[-1].group(1), sha))
                    elif paths:
                        counts["unclassified"] += 1
                        info.append(
                            "unclassified %s:%d -> %s %s `%s` (declare the genre: add"
                            " a cue -- read at / as of / measured at -- or make it a"
                            " pin by removing the words between)"
                            % (rel_doc, lineno, paths[-1].group(1), cue.strip() or
                               "<adjacent?>", sha))
                    else:
                        # Text *before* the sha, not after it: the doc's own word
                        # for a row id ("row `<id>`", "snapshot `<id>`") precedes
                        # the token, so a parenthetical "(row id)" written after it
                        # is not seen -- the window is named for the side it reads
                        # (app's find, 2026-10-17).
                        before = ctx[max(0, m.start() - 240):m.start()]
                        if commit_exists(root, sha):
                            # A sha with no qualifier in this repo's own doc is
                            # this repo's by default; nothing to attribute.
                            counts["repo-ok"] += 1
                            if not reachable_refs(root, sha):
                                counts["repo-unreferenced"] += 1
                                info.append(
                                    "repo-unreferenced %s:%d -> `%s` is reached by no"
                                    " origin ref or tag in this repo"
                                    % (rel_doc, lineno, sha))
                            continue
                        if ROWISH.search(before):
                            # A snapshot/window row id reads exactly like a short
                            # sha; the doc's own words are what say it is not a
                            # revision, so the guard takes them.
                            counts["rowid-skipped"] += 1
                            continue
                        hits = list(REPO_TOK.finditer(before))
                        repo = hits[-1] if hits else None
                        tree = next((r for r in roots if r.name == "crm-cdp-" +
                                     (repo.group(1) or repo.group(2)).lower()),
                                    None) if repo else None
                        if tree is not None and commit_exists(tree, sha):
                            counts["repo-ok"] += 1
                            if not reachable_refs(tree, sha):
                                counts["repo-unreferenced"] += 1
                                info.append(
                                    "repo-unreferenced %s:%d -> `%s` exists in %s but"
                                    " no origin ref or tag reaches it (unpushed or"
                                    " deleted branch: a fresh clone cannot resolve"
                                    " it)" % (rel_doc, lineno, sha, tree.name))
                        elif tree is not None:
                            elsewhere = [r.name for r in roots if r is not tree
                                         and commit_exists(r, sha)]
                            if elsewhere:
                                # The nearest repo name is not the owner: not an
                                # error, but a reader would attribute it wrongly.
                                counts["repo-elsewhere"] += 1
                                info.append(
                                    "repo-elsewhere %s:%d -> `%s` sits nearer %s but"
                                    " is a commit in %s"
                                    % (rel_doc, lineno, sha, tree.name,
                                       ", ".join(elsewhere)))
                            else:
                                # Not in any tree: another system's hash (a uuid
                                # prefix reads like a short sha) or a repo that
                                # is not checked out here.
                                counts["no-commit"] += 1
                                info.append(
                                    "no-commit  %s:%d -> `%s` is UNVERIFIABLE here:"
                                    " no checked-out tree holds it (another"
                                    " system's hash, a repo not checked out, or a"
                                    " sha this clone never fetched) -- absence of a"
                                    " verdict is not a pass"
                                    % (rel_doc, lineno, sha))
                        else:
                            counts["unclassified"] += 1
                            info.append(
                                "unclassified %s:%d -> `%s` with neither a path nor a"
                                " repo name (name one: repo+sha is checkable for"
                                " existence, path+sha for equality)"
                                % (rel_doc, lineno, sha))
    return counts, hard, info


def basename_index(roots: list[Path]) -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = {}
    for root in roots:
        files = repo_files(root)
        if files is None:
            files = [p for p in root.rglob("*")
                     if p.is_file() and not any(part in SKIP_DIRS
                                                for part in p.parts)]
        for p in files:
            if p.suffix.lstrip(".") in EXTS:
                idx.setdefault(p.name, []).append(p)
    return idx


def audit(root: Path, peers: list[Path], strict: bool,
          comments: bool = False) -> int:
    roots = [root, *peers]
    idx = basename_index(roots)
    counts = {"ok": 0, "disagrees": 0, "out-of-range": 0, "unresolved": 0}
    hard: list[str] = []
    info: list[str] = []

    for doc in iter_md(root, comments):
        rel_doc = doc.relative_to(root)
        with doc_units(doc, comments) as units:
            for lineno, text in units:
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
    pin_counts, pin_hard, pin_info = audit_pins(root, roots, idx, comments)
    audited = list(iter_md(root, comments))
    print("trees: " + ", ".join(str(r) for r in roots if r.is_dir()))
    print("coverage: %d file(s) -- %s" % (
        len(audited),
        "markdown + comment lines/docstrings in .py/.sql" if comments else
        "markdown only: cites in Python docstrings and SQL comments are outside"
        " this pass, so green means green over one file type (add --comments)"))
    print("citations: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("pins: " + ", ".join(f"{k}={v}" for k, v in pin_counts.items()))
    print("  (a pin asserts equality with the file's last-changed revision; a"
          " label proves existence + ancestry, and with a path that the path was"
          " there at that sha -- never that the quoted words are)")
    print("  (*-unreferenced = reached by no origin ref or tag: a fresh clone"
          " cannot resolve it and the object dies at the next gc, so it is a"
          " clock on the reader, not an error in the cite)")
    # `hard` is never capped (it is the exit condition); the informational
    # stream is, so the findings above cannot be buried by a long tail.
    shown = info + pin_info
    for line in shown[:40] + hard + pin_hard:
        print("  " + line)
    if len(shown) > 40:
        print("  ... (+%d more informational)" % (len(shown) - 40))
    return 1 if (hard or pin_hard) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit markdown code-line citations.")
    ap.add_argument("--repo", default=".", help="repo whose markdown to audit")
    ap.add_argument("--peer", nargs="*", default=None,
                    help="trees to resolve cites against (default: sibling "
                         "crm-cdp-app / crm-cdp-ml if present)")
    ap.add_argument("--strict", action="store_true",
                    help="also fail when a cited path is in no tree")
    ap.add_argument("--comments", action="store_true",
                    help="also read cites in `#`/`--` comments and docstrings")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if args.peer is None:
        peers = [p for p in (root.parent / "crm-cdp-app", root.parent / "crm-cdp-ml")
                 if p.is_dir()]
    else:
        peers = [Path(p).resolve() for p in args.peer]
    return audit(root, peers, args.strict, args.comments)


if __name__ == "__main__":
    sys.exit(main())
