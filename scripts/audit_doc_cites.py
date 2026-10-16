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

**The verdict is only as good as the universe it was computed in, and a wrong
universe is indistinguishable from a defective cite** (app, ml, 2026-10-17): run
from app's root without ``--peer``, the old hardcoded default listed app twice
and left data out, and app's own tree then reported ``unresolved=10``,
``pin-unresolved=1``, ``unclassified=4`` — fifteen findings, all fifteen the
invocation, in the same buckets with the same wording and ``exit 0``. So the peer
set is printed beside the verdict, the default is *the sibling
``crm-cdp-{app,data,ml}`` trees that exist, minus the audited root*,
``--require-peer`` makes an absent tree a hard failure, and a declared peer that
is missing prints ``peer-missing <path>`` (hard under ``--strict``).
``unclassified`` is the worst of the three universe-dependent buckets:
``unresolved`` at least says "path in no tree", while ``unclassified`` reads as a
*shape* defect and would send a reader to rewrite a correct citation.

Read-only, stdlib only. Exit 1 on a hard failure (a cite out of range in every
tree holding it, a stale pin; under ``--strict``, a path in no tree or a declared
peer that is absent; always, a ``--require-peer`` that is absent).

    python scripts/audit_doc_cites.py
    python scripts/audit_doc_cites.py --comments
    python scripts/audit_doc_cites.py --strict
    python scripts/audit_doc_cites.py --require-peer ../crm-cdp-app

``--comments`` extends the same genres to the places the cites actually live:
comment lines (``#`` in .py, ``--`` in .sql) and triple-quoted string bodies,
where ml's and app's anchors are. Boundaries are stated in the output rather
than left to the reader: a default run is a pass over **markdown only** (with
the outside surface counted, so a zero is bounded), a trailing ``# …`` after
code is not read because attributing it means guessing, and a range cite whose
naming identifier sits on a *neighbouring* line is counted in
``cite-symbol-unchecked`` instead of being resolved -- the window-scoped version
of that fix flagged correct cites, so the sites are listed for hand-reading.
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
# A path token may be backticked or bare: `db/migrations/031_x.sql `412d9d7`` is a
# pin and so is the same pair with backticks on both -- the parents are
# adjacency and one line, not punctuation (app, 2026-10-17: reading "bare" as
# "no backticks between" left a real pin unparsed, so its sha fell through to
# `repo-ok`, a success bucket, and the under-check had no tell in the counts or
# in the exit code).
PATH_BARE = r"[\w./-]+\.(?:" + "|".join(EXTS) + r")(?![A-Za-z0-9])"
PATH_TOK = r"(?:`[\w./-]+\.(?:" + "|".join(EXTS) + r")`|" + PATH_BARE + r")"
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
# A file is "citation-shaped" if it carries a token **this guard would read**: its
# own two requirements -- a backticked sha, or a path token followed by a line
# number -- and nothing else, so a scope count and the pass cannot drift apart
# about what a citation is. A hex-shaped grep is a different and worse
# classifier: it printed 41 files in app's tree where the honest number is 4
# (their four pins), which is the failure this whole guard exists to prevent,
# one level up (app, 2026-10-17).
# `[0-9]+` rather than `\d+` on purpose: this pattern is also handed to `git
# grep -E` for the files outside the pass, and `\d` is a GNU-ERE extension that
# silently matches a literal `d` -- the first version of this counter printed
# "0 outside" against a tree with four pin-bearing files, i.e. a vacuous zero
# produced by the counter itself.
CITATION_SHAPE = re.compile(SHA_TOK.pattern + "|" + PATH_TOK + r":[0-9]+")
# The same idea, handed to `git grep -E` for the files outside the pass. POSIX
# ERE has no non-capturing groups, no `\d`, and no lookahead, and an invalid
# pattern exits 128 -- which the first version of this counter read as "not a
# repo" and turned into a vacuous "0 outside" against a tree whose four pins
# were exactly what it was counting.
GREP_CITATION_SHAPE = (CITATION_SHAPE.pattern.replace("(?:", "(")
                       .replace("(?![A-Za-z0-9])", ""))


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


def _unmark(raw: str, prefixes: tuple[str, ...]) -> str:
    """Drop one leading comment marker, so a wrapped anchor reads as one span.

    A pin may straddle a wrap (``path`` on one line, its ``sha`` on the next).
    In markdown the join is a plain newline, but a comment unit keeps its
    ``-- ``/``# `` marker, which sits *inside* the window the pin scan builds --
    so ``\\s*`` cannot span it and the sha fell silently through to ``repo-ok``,
    a success bucket (app, 2026-10-17). Stripping at the source keeps
    ``base = len(ctx) - len(text)`` exact, because both sides are stripped.
    """
    m = re.match(r"\s*(?:" + "|".join(re.escape(p) for p in prefixes) + r")\s?",
                 raw)
    return raw[m.end():] if m else raw


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
        body = _unmark(raw, prefixes)
        if quote is not None:
            units.append((lineno, body))
            if stripped.count(quote) % 2 == 1:
                quote = None
            continue
        opener = next((q for q in ('"""', "'''") if stripped.startswith(q)), None)
        if opener is not None:
            units.append((lineno, body))
            if stripped.count(opener) % 2 == 1:
                quote = opener
            continue
        if any(stripped.startswith(pre) for pre in prefixes):
            units.append((lineno, body))
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


def _peers(roots: list[Path]) -> str:
    """The searched universe, printed beside the numbers it decides.

    ``unresolved``, ``pin-unresolved`` and ``unclassified`` are all decided
    against the peer set, so a pasted finding has to carry it (app, 2026-10-17:
    the note was printed only when such a bucket was non-zero, so a clean run
    never showed it and a quoted line never did either).
    """
    return ", ".join(r.name for r in roots[1:]) or "(none)"


def _unparsed_cues(text: str) -> list[str]:
    """Cue words whose sha is not a token the guard can check.

    ml's four labels sat in prose (``read at data f889386``) and so were checked
    by nothing while the run reported green (ml, 2026-10-17): an unchecked claim
    that reads exactly like a checked one is the failure mode this whole guard is
    about, so it gets a bucket instead of silence.
    """
    if not LABEL_CUE.search(text):
        return []
    stripped = SHA_TOK.sub(" ", text)
    return re.findall(r"(?<![`\w])([0-9a-f]{7,40})(?![`\w])", stripped)


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
    counts["pin-sites"] += 1  # sites, not verdicts: `seen` keys on (path, rev)
    if (path, pinned) in seen:
        return
    seen.add((path, pinned))
    found = [(r, rel, last_changed(r, rel)) for r, rel in owner_trees(path, roots, idx)]
    found = [(r, rel, got) for r, rel, got in found if got]
    if len({got for _, _, got in found}) > 1:
        # A path held by more than one tree makes "the file's last change"
        # tree-relative -- app's tree mirrors data's migrations byte-identically,
        # so the same bare pair reads `pin-ok` with data present and `stale-pin`
        # with only app present. The pair must stay bare (the genre is the bare
        # pair), so the owning tree belongs in the surrounding prose; this line
        # says the pin is ambiguous instead of picking an owner silently.
        counts["pin-ambiguous"] += 1
        info.append(
            "pin-ambiguous %s:%d -> %s exists in %s; a pin's authority is the"
            " tree its sha belongs to, so name the owning tree in the prose"
            % (rel_doc, lineno, path,
               ", ".join("%s=%s" % (r.name, got) for r, _, got in found)))
    if not found:
        counts["pin-unresolved"] += 1
        info.append(f"pin-unresolved {rel_doc}:{lineno} -> {path} (path in no tree;"
                    f" peers checked: {_peers(roots)})")
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
              "sentinel-skipped": 0, "rowid-skipped": 0, "cue-unparsed": 0,
              "pin-ambiguous": 0, "pin-sites": 0}
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
                for bare in _unparsed_cues(text):
                    counts["cue-unparsed"] += 1
                    info.append(
                        "cue-unparsed %s:%d -> `%s` sits in a cue phrase but is not"
                        " a backticked token, so NO revision was checked (backtick"
                        " it: the guard reads tokens)" % (rel_doc, lineno, bare))
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
                            " pin by removing the words between; peers checked: %s)"
                            % (rel_doc, lineno, paths[-1].group(1), cue.strip() or
                               "<adjacent?>", sha, _peers(roots)))
                    else:
                        # Text *before* the sha, not after it: the doc's own word
                        # for a row id ("row `<id>`", "snapshot `<id>`") precedes
                        # the token, so a parenthetical "(row id)" written after it
                        # is not seen -- the window is named for the side it reads
                        # (app's find, 2026-10-17).
                        before = ctx[max(0, m.start() - 240):m.start()]
                        # Repo attribution is a local, explicit signal; a generic
                        # row-ish word 200 characters back is neither -- and
                        # `rowid-skipped` is the one bucket that can swallow a
                        # checkable sha without printing anything, which reads as
                        # a pass. So the explicit signal is tried first and the
                        # row-ish branch reports any token that is a commit
                        # somewhere (app, 2026-10-17).
                        hits = list(REPO_TOK.finditer(before))
                        repo = hits[-1] if hits else None
                        named = next((r for r in roots if r.name == "crm-cdp-" +
                                      (repo.group(1) or repo.group(2)).lower()),
                                     None) if repo else None
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
                        if ROWISH.search(before) and not (
                                named is not None and commit_exists(named, sha)):
                            # A snapshot/window row id reads exactly like a short
                            # sha; the doc's own words are what say it is not a
                            # revision, so the guard takes them -- unless a named
                            # repo actually holds it, which settles the question.
                            counts["rowid-skipped"] += 1
                            owners = [r.name for r in roots if commit_exists(r, sha)]
                            if owners:
                                info.append(
                                    "rowid-checkable %s:%d -> `%s` read as a row id,"
                                    " yet it is a commit in %s (a named repo would"
                                    " have checked it)" % (rel_doc, lineno, sha,
                                                            ", ".join(owners)))
                            continue
                        tree = named
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
                                " existence, path+sha for equality; peers checked:"
                                " %s)" % (rel_doc, lineno, sha, _peers(roots)))
    return counts, hard, info


# A backticked name that reads as a *symbol* rather than a word: it carries an
# underscore or an internal capital (`resolve_ld8_window`, `RULES_V2`,
# `createRfmSnapshot`). The shape matters -- `python`, `git`, `snapshot` are
# words that happen to be backticked, and treating them as code names would
# report a moved cite on every sentence that mentions a tool.
SYMBOL = re.compile(r"`([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*)`")


def _defines(line: str, name: str) -> bool:
    """Is `name` *defined* on this line, rather than merely mentioned?

    The first cut of the detector fired on prose that named a field
    (`as_of`) which the file uses everywhere and defines nowhere: mentioning a
    name is not citing an object. Requiring a defining line (`def`/`class`,
    or `NAME =`) is what makes the hit mean "the object is here, not there".
    """
    n = re.escape(name)
    return bool(re.match(r"\s*(?:async\s+)?(?:def|class)\s+" + n + r"\b", line)
                or re.match(r"\s*" + n + r"\s*[:=]", line))


# A line that *defines* something: the second half of the detector, because
# "the sentence names X" only matters when the cited span is about Y.
DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"
                    r"|^\s*([A-Za-z_]\w*)\s*[:=]")


def _encloses(lines: list[str], def_line: int, span_start: int) -> bool:
    """Does the block opened at `def_line` (1-based) contain `span_start`?

    Indentation, not `ast`, because the cited file may be `.sql` -- and this
    answers only "is the span inside that definition's body", which is the
    question the allowance below asks.
    """
    if def_line >= span_start:
        return False
    head = lines[def_line - 1]
    indent = len(head) - len(head.lstrip())
    for ln in lines[def_line:span_start - 1]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) <= indent:
            return False
    return True


def _window(units: list[tuple[int, str]], i: int, start: int, end: int,
            back: int = 120, fwd: int = 120) -> str:
    """Text around a cite: its **own clause**, capped at ~120 characters a side.

    A naming identifier sits after its cite as often as before it: app's
    `snapshots_serve.py:656-657` cites a comment inside `compute_current` while
    the backticked name is on the following line. Line-scoped, the candidate
    set is empty and the span goes *unchecked* -- the loss is silence, which is
    the one outcome this guard must not produce (app, 2026-10-17).

    The window is deliberately *tight*, and that is a measurement, not a
    preference: the paragraph-sized version of this fix reported two correct
    cites in `pipelines/contracts.md` (`:717` on `tests/test_ld8.py:43`, `:817`
    on `pipelines/schedule.py:255-263`) because identifiers from the *rest of
    the paragraph* -- a test name, a parameter -- joined the candidate set and
    were then found absent from the span. A bucket that fires on correct cites
    is as useless as one that hides bad ones, so the radius is characters
    around the cite, not the paragraph around the line.
    """
    pre = ("\n".join(t for _, t in units[max(0, i - 2):i]) + "\n"
           + units[i][1][:start])[-back:]
    post = (units[i][1][end:] + "\n"
            + "\n".join(t for _, t in units[i + 1:i + 3]))[:fwd]
    return pre + units[i][1][start:end] + post


def _symbols(text: str) -> set[str]:
    """Symbol-shaped backticked identifiers in `text` -- the candidate subjects."""
    return {n for n in SYMBOL.findall(text) if n}


def symbol_moved(roots: list[Path], cited: str, text: str, start: int,
                 end: int, rel_doc: str, lineno: int,
                 window: str = "") -> str | None:
    """A range cite proving the range exists, never that it bounds the subject.

    A peer cited `<file>.py:101-107` for `<symbol>` while those lines are
    `<other statement>` and the delegation is 20 lines lower (app, 2026-10-17):
    in range, so `ok` and `out-of-range=0` were both true while the sentence
    pointed at a different object. Written with schematic names on purpose -- an
    example carrying live values is itself a citation, and this docstring's
    first version was reported by its own guard (2026-10-17).

    Three filters keep the hit honest: the sentence must name a symbol-shaped
    token **on the cite's own line**, and the cited span must *define* some
    other name -- otherwise a sentence that names a statement while deliberately
    citing part of it (contracts.md:475 cites the scope conjuncts `:63-66` of
    `SNAPSHOT_FIND_V2_SQL`) would be reported as a defect, and a bucket that
    fires on correct cites is as useless as one that hides bad ones -- and a
    candidate that *encloses* the span is the subject, not a mismatch (app,
    2026-10-17: their `:656-657` names the enclosing `compute_current`).

    `window` is accepted but not used as a candidate source: candidates are
    line-scoped because the window-scoped version fired on two correct cites in
    `pipelines/contracts.md` (measured 2026-10-17), and the caller counts those
    sites as `cite-symbol-unchecked` instead.
    """
    names = sorted(_symbols(text))
    if not names:
        return None
    for tree in roots:
        f = tree / cited
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        span_lines = lines[start - 1:end]
        if any(n in "\n".join(span_lines) for n in names):
            return None
        here = []
        for ln in span_lines:
            d = DEF_RE.match(ln)
            if d:
                here.append(d.group(1) or d.group(2))
        if not here:
            return None  # the span defines nothing: it is part of the subject
        at = [i + 1 for i, ln in enumerate(lines)
              if any(_defines(ln, n) for n in names)]
        if not at:
            return None  # the named object is not here at all, so not this cite's
        # A cite may name the **enclosing definition** rather than a line in the
        # span: app's `:656-657` names `compute_current`, whose `def` is at
        # `:108` and whose body contains the cited comment at `:123`. That is a
        # correct cite, and app withdrew the window refinement on exactly this
        # measurement -- so an identifier that *contains* the span is the cite's
        # subject, not a mismatch (app, 2026-10-17).
        for n in names:
            for ln_no, ln in enumerate(lines, start=1):
                if _defines(ln, n) and _encloses(lines, ln_no, start):
                    return None
        return ("cite-symbol %s:%d -> %s:%d-%d in %s names %s, which the span"
                " does not contain -- it defines %s (the named object is at"
                " :%s)"
                % (rel_doc, lineno, cited, start, end, tree.name,
                   " or ".join("`%s`" % n for n in names[:3]),
                   " or ".join("`%s`" % n for n in here[:2]),
                   ", :".join(str(i) for i in at[:4])))
    return None


# Which file types each pass actually reads. A count is bounded by this set,
# and the set is what `coverage:` prints.
PASS_EXTS = {False: {".md"}, True: {".md", ".py", ".sql"}}


def outside_pass(root: Path, comments: bool) -> tuple[int, list[str], str]:
    """(files skipped by this pass, citation-shaped among them, a caveat)."""
    """Files outside this pass that are citation-shaped -- i.e. files the
    ``--comments`` pass would have something to check in.

    `coverage: 3 file(s) -- markdown only` with every bucket at zero is honest
    and still misleading when four `.sql` files outside the pass hold pins: the
    identical zero means "nothing to check" in one tree and "the checks are
    elsewhere" in another, and only the outside surface separates them (app,
    2026-10-17: theirs was the wrong one, not the vacuous one).

    The predicate is ``CITATION_SHAPE``, the guard's own token requirement, not
    a hex-shaped grep -- and it uses the same regex object, so there is one
    definition of "citation-shaped" in the file rather than two that can drift.
    Measured against the mode boundary: a run skips whole file *types*, so the
    honest count is how many of those types carry citations, not how many carry
    hex. Both sides are printed as a ratio of files-in / files-with-citations,
    because those are different claims and only the file count catches a whole
    file dropping out of the pass.
    """
    if comments:
        return 0, [], ""  # every file type the guard knows is already in pass
    files = repo_files(root)
    if files is None:
        return 0, [], "not a git checkout, so the outside surface is unknown"
    skipped = [p for p in files if p.suffix in {".py", ".sql"}]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "grep", "-lIE", GREP_CITATION_SHAPE,
             "--", "*.py", "*.sql"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return len(skipped), [], "git grep could not run (%s)" % type(exc).__name__
    if out.returncode not in (0, 1):  # 1 = no match; 2/128 = bad pattern / no repo
        # Never `return []` here: an invalid pattern exits 128, which reads as
        # "no citations outside" -- a vacuous zero manufactured by the counter
        # itself, which is the exact failure this whole guard exists to catch.
        return len(skipped), [], ("git grep failed (rc=%d), so the outside surface"
                                  " is unknown" % out.returncode)
    return len(skipped), [n for n in out.stdout.splitlines() if n], ""


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
          comments: bool = False, peer_notes: list[str] | None = None,
          peer_hard: list[str] | None = None,
          absent: list[str] | None = None) -> int:
    roots = [root, *peers]
    idx = basename_index(roots)
    universe0 = _peers(roots)
    counts = {"ok": 0, "disagrees": 0, "out-of-range": 0, "unresolved": 0,
              "cite-symbol": 0, "cite-symbol-unchecked": 0}
    hard: list[str] = list(peer_hard or [])
    info: list[str] = list(peer_notes or [])

    for doc in iter_md(root, comments):
        rel_doc = doc.relative_to(root)
        with doc_units(doc, comments) as units:
            for i, (lineno, text) in enumerate(units):
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
                            f"(path in no tree; peers checked: {_peers(roots)})")
                    elif all(ok for _, _, ok in where):
                        counts["ok"] += 1
                        win = _window(units, i, m.start(), m.end())
                        moved = symbol_moved(roots, cited, text, start, end,
                                             rel_doc, lineno, window=win)
                        if moved:
                            counts["cite-symbol"] += 1
                            info.append(moved)
                        elif (not _symbols(text) and _symbols(win)
                              and len(CITE.findall(win)) <= 1):
                            # `<= 1` = this cite is the window's only one. With a
                            # second cite in the window its own identifier is
                            # the neighbour, and reporting it would bury the
                            # sites that genuinely have one.
                            # A naming identifier exists, but on a *neighbouring*
                            # line: the span is then unchecked, and silence is
                            # the one outcome this guard must not produce. It is
                            # reported rather than resolved, because widening
                            # the candidate set to the window flags correct
                            # cites (`pipelines/contracts.md:369` on
                            # `store_segments.py:174-181`, `:817` on
                            # `schedule.py:255-263` -- measured 2026-10-17).
                            counts["cite-symbol-unchecked"] += 1
                            info.append(
                                "cite-symbol-unchecked %s:%d -> %s (no naming"
                                " identifier on the line; the next line offers"
                                " %s -- the span is unchecked until they sit"
                                " together, so this is a site to read by hand)"
                                % (rel_doc, lineno, span,
                                   ", ".join(sorted(_symbols(win))[:2])))
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
    sites = pin_counts.pop("pin-sites", 0)
    universe = universe0
    if absent:
        universe += " + absent: " + ", ".join(absent)
    print("trees: " + ", ".join(str(r) for r in roots if r.is_dir()))
    print("peers: " + universe)
    # The unified form, stated where the numbers are read: a byte-compare
    # measures the reader's checkout, a lint run the reader's peer set, a
    # `--stat` the revision the reader had, and a guard run which files it
    # looked at. Every verdict here is scoped by a state of the runner, and the
    # scope belongs beside the number it bounds -- including when it is zero.
    print("  (scope: every count below is decided by the peers printed above"
          " and by the coverage mode below; a number pasted without them is a"
          " claim about the harness, not about the docs)")
    n_skipped, outside, outside_note = outside_pass(root, comments)
    in_shape = sum(
        1 for p in audited
        if any(CITATION_SHAPE.search(t) for _, t in text_units(p, comments)))
    if outside_note:
        out_txt = "; %d file(s) outside this pass, citations UNKNOWN (%s)" % (
            n_skipped, outside_note)
    else:
        out_txt = "; %d file(s) outside this pass (%d contain citations)" % (
            n_skipped, len(outside))
    print("coverage: %d file(s) in pass (%d contain citations)%s -- %s" % (
        len(audited), in_shape, out_txt,
        "markdown + comment lines/docstrings in .py/.sql" if comments
        else "markdown only: cites in Python docstrings and SQL comments are"
        " outside this pass, so green means green over one file type (add"
        " --comments)"))
    # The scope rides on the counts line as well as on `coverage:`, because the
    # counts line is the one that gets pasted into a chat -- and a clean run is
    # pasted alone far more often than a dirty one.
    mode = "markdown + comments" if comments else "markdown only"
    print("citations: " + ", ".join(f"{k}={v}" for k, v in counts.items())
          + "  [pass: %d file(s), %s; peers: %s]" % (len(audited), mode,
                                                      universe))
    pins_line = ", ".join(f"{k}={v}" for k, v in pin_counts.items())
    if sites > pin_counts["pin-ok"]:
        # One verdict per (path, revision) pair, but several sites may pin the
        # same pair: a reader reconciling against a grep count needs both.
        pins_line = pins_line.replace(
            "pin-ok=%d" % pin_counts["pin-ok"],
            "pin-ok=%d (%d sites)" % (pin_counts["pin-ok"], sites), 1)
    if not comments:
        # `pin-ok=0` reads like "no pins here" while the anchors may all live in
        # comments this pass never opened, so the scope rides on the number.
        pins_line += "  [markdown only: .py/.sql comment anchors need --comments]"
    print("pins: " + pins_line)
    print("  (a pin asserts equality with the file's last-changed revision; a"
          " label proves existence + ancestry, and with a path that the path was"
          " there at that sha -- never that the quoted words are)")
    print("  (*-unreferenced = reached by no origin ref or tag: a fresh clone"
          " cannot resolve it and the object dies at the next gc, so it is a"
          " clock on the reader, not an error in the cite)")
    # Printed only when such a bucket is non-zero, so a clean run stays short:
    # the number in these three buckets is decided by the peer set, not only by
    # the text, and the reader must be told that where the number is read.
    if (counts["unresolved"] or pin_counts["pin-unresolved"]
            or pin_counts["unclassified"]):
        print("  (peers: %s -- unresolved, pin-unresolved and unclassified are"
              " decided against that universe: a cite that lives only in an"
              " absent tree reports as missing or unclassifiable, so a short"
              " universe is the first hypothesis, not the last)" % universe)
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
    ap.add_argument("--repo", default=".",
                    help="repo whose markdown to audit; harness state like "
                         "--peer, so it is printed as the first `trees:` entry "
                         "and the audited root is never its own peer -- every "
                         "count below is about this tree and the peers it was "
                         "resolved against. `coverage:` counts FILES, the "
                         "bucket lines count CITATIONS: `ok=5` over 3 files and "
                         "`ok=5` over 5 files are different claims, and the file "
                         "count is the one that catches a whole file leaving "
                         "the pass")
    # `action="append"` + flatten, not a bare `nargs="*"`: with the latter a
    # repeated flag silently overwrites, so `--peer data --peer ml` registered
    # only ml and the run manufactured findings out of a short universe (app,
    # 2026-10-17: `unresolved` x10, `pin-unresolved` x2, `stale-pin` x1, all
    # invocation-caused). Repetition is honoured or it is a trap.
    ap.add_argument("--peer", action="append", nargs="*", default=None,
                    help="trees to resolve cites against (default: the sibling "
                         "crm-cdp-app / crm-cdp-data / crm-cdp-ml trees that "
                         "exist, minus the audited root); repeatable. REPLACES "
                         "the default set rather than adding to it, so an "
                         "explicit list must be complete: a missing tree turns "
                         "its cites into `unresolved`/`label-unknown` and the "
                         "findings are the invocation's, not the docs'")
    ap.add_argument("--require-peer", action="append", default=[], metavar="PATH",
                    help="a tree that must be present (repeatable); absent -> "
                         "exit 1, for CI where a forgotten peer must not read "
                         "as a shorter report")
    ap.add_argument("--strict", action="store_true",
                    help="also fail when a cited path is in no tree, or when a "
                         "declared --peer is absent")
    ap.add_argument("--comments", action="store_true",
                    help="also read cites in `#`/`--` comments and docstrings")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if args.peer is None:
        named = [root.parent / n for n in
                 ("crm-cdp-app", "crm-cdp-data", "crm-cdp-ml")]
    else:
        named = [Path(p) for group in args.peer for p in group]
    required = [Path(p) for p in args.require_peer]

    # The audited root is never its own peer: from app's root the old default
    # named app twice and omitted data, and fifteen invocation-caused findings
    # then read exactly like text defects.
    peers: list[Path] = []
    seen: set[Path] = set()
    for p in named + required:
        r = p.resolve()
        if r == root or r in seen:
            continue
        seen.add(r)
        if r.is_dir():
            peers.append(r)

    info: list[str] = []
    hard: list[str] = []
    absent: list[str] = []
    for p in named:
        r = p.resolve()
        if not r.is_dir():
            absent.append(r.name)
            msg = ("peer-missing %s (declared): a cite that lives only there "
                   "reports as unresolved or unclassified, so a short universe "
                   "reads like a defective cite" % r)
            (hard if args.strict else info).append(msg)
    for p in required:
        r = p.resolve()
        if not r.is_dir():
            absent.append(str(r))
            hard.append("peer-missing %s (--require-peer): this run was told "
                        "that tree must be present" % r)
    return audit(root, peers, args.strict, args.comments, info, hard, absent)


if __name__ == "__main__":
    sys.exit(main())
