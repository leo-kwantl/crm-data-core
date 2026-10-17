"""The cite guard's *harness* behaviour, which its counts depend on.

Every bucket in `scripts/audit_doc_cites.py` is decided by the invocation, not
only by the docs: `--peer` **replaces** the default peer set (so declaring one
peer turns the others' cites into findings that look like text defects), and
`--comments` decides whether comment-line cites exist at all. Both are printed
beside the counts for exactly that reason, and `d61978b` added a third: the
checkout the caller *stands in* is dropped from the peer set by the same
replace-not-add rule, so the tree that is most obviously meant is the easiest
to omit.

These tests pin the printed affordances, not the bucket arithmetic: a warning
that only exists because someone remembered to print it is not a check.
Synthetic trees only -- no git, no sibling checkouts, no DB.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[3] / "scripts" / "audit_doc_cites.py"


@pytest.fixture()
def trees(tmp_path: Path) -> dict[str, Path]:
    """Three minimal trees: the audited one, a declared peer, the caller's cwd.

    `other/.git` is an empty directory: the note only asks whether the caller's
    cwd looks like a checkout, so the test does not need a real repository.
    """
    proj = tmp_path / "proj"
    peer = tmp_path / "peer"
    other = tmp_path / "other"
    for path in (proj, peer, other / ".git"):
        path.mkdir(parents=True, exist_ok=True)
    (proj / "d.md").write_text("See `p.md:1` for the thing.\n", encoding="utf-8")
    (peer / "p.md").write_text("hello\n", encoding="utf-8")
    return {"proj": proj, "peer": peer, "other": other}


def run(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(GUARD), *args],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_standing_in_an_undeclared_tree_is_named(trees: dict[str, Path]) -> None:
    out = run(trees["other"], "--repo", "../proj", "--peer", "../peer")
    assert "note: you are standing in other, which is not a declared peer" in out
    # The note is worth printing only next to the peer set it warns about: the
    # same line has to carry the harness, or the warning is about nothing.
    counts = [line for line in out.splitlines() if line.startswith("citations:")]
    assert len(counts) == 1 and "peers: peer" in counts[0]


def test_declaring_the_caller_tree_silences_the_note(trees: dict[str, Path]) -> None:
    out = run(trees["other"], "--repo", "../proj", "--peer", "../peer", "--peer", ".")
    assert "note: you are standing" not in out


def test_auditing_from_the_audited_tree_has_no_note(trees: dict[str, Path]) -> None:
    out = run(trees["proj"], "--repo", ".", "--peer", "../peer")
    assert "note: you are standing" not in out


def test_every_verdict_line_carries_the_harness(trees: dict[str, Path]) -> None:
    """`coverage:`, `citations:` and `pins:` each name the mode and the peer set.

    `pins:` was the line missing it, and it is the one with the most
    peer-dependent buckets: a label that does not resolve in a declared peer is
    reported as `label-bad`, i.e. as a defect of the docs rather than of the
    invocation, so a pasted `pins:` line without the peer set is exactly the
    under-specification the rule forbids.
    """
    out = run(trees["other"], "--repo", "../proj", "--peer", "../peer", "--peer", ".")
    lines = {line.split(":", 1)[0]: line for line in out.splitlines()
             if line[:1].isalpha()}
    for name in ("coverage", "citations", "pins"):
        assert name in lines, out
        assert "pass: " in lines[name], lines[name]
        assert "peers: peer" in lines[name], lines[name]
