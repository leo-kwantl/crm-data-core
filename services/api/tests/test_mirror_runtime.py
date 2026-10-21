"""The shared runtime files must byte-equal app's checkout (A114, part 3).

Data's rule (AGENTS.md § Repo rules): for any file present in **both**
trees, app's checkout is the base — sync it byte-for-byte, never fork it.
That rule had no check, so the twenty shared runtime files under
`services/api/` drifted: the manager measured 20 of the 68 shared `.py`
files differing on 2026-10-22, including the session rule itself. A
mirrored file with no mirror test is a copy, and a copy drifts — that is a
measured fact now, not a caution.

The comparison is on the normalised text (CRLF -> LF) against app's HEAD
blob: both trees are checked out with `core.autocrlf=true`, so a worktree
BYTE comparison is a false alarm ("compare blobs, not worktrees"), and a
sibling's dirty working tree is not a shipped state, so a suite that
depended on it would be red in one direction for a change nobody shipped.

`services/api/tests/**` is data's own harness and is excluded by rule.
App's own mirror guard (`services/api/tests/test_mirror_modules.py`) is the
pattern for the other direction; it covers one module today.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
APP_REPO = Path(os.environ.get("APP_REPO_PATH", REPO.parent / "crm-cdp-app"))
_SIBLING_ABSENT = not (APP_REPO / "services" / "api").is_dir()


def _normalised(text: str) -> str:
    return text.replace("\r\n", "\n")


def _skip_or_fail() -> None:
    """Absent sibling repo: skip by default, fail when MIRROR_REQUIRED=1."""
    if not _SIBLING_ABSENT:
        return
    msg = (f"crm-cdp-app not checked out (looked in {APP_REPO}; "
           f"set APP_REPO_PATH)")
    if os.environ.get("MIRROR_REQUIRED", "").strip().lower() not in (
            "", "0", "false", "no"):
        pytest.fail(f"MIRROR_REQUIRED=1 but {msg}", pytrace=False)
    pytest.skip(msg)


def _ls_files(repo: Path) -> set[str]:
    # The bytes are decoded as UTF-8 by hand: `text=True` would use the
    # host's locale codec (cp950 here) and blow up on the first em dash.
    proc = subprocess.run(["git", "-C", str(repo), "ls-files", "services/api"],
                          capture_output=True)
    if proc.returncode != 0:
        return set()
    return {p for p in proc.stdout.decode("utf-8", "replace").split()
            if p.endswith(".py") and "/tests/" not in p}


def _app_head(rel: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(APP_REPO), "show", f"HEAD:{rel}"],
                          capture_output=True)
    if proc.returncode != 0:
        return None
    return _normalised(proc.stdout.decode("utf-8", "replace"))


def _shared_files() -> list[str]:
    """The `.py` files under services/api/ that BOTH trees carry."""
    return sorted(_ls_files(APP_REPO) & _ls_files(REPO))


def _first_difference(mine: str, theirs: str) -> int:
    return next((i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b),
                min(len(mine), len(theirs)))


def test_the_session_runtime_is_app_s_copy() -> None:
    """The session rule lives once: app's `scope.py` and `deps.py` are the
    base, and data's copies are the same file."""
    _skip_or_fail()
    for rel in ("services/api/analytics/scope.py", "services/api/deps.py"):
        theirs = _app_head(rel)
        assert theirs is not None, f"{rel} is not in app's tree"
        mine = _normalised((REPO / rel).read_text(encoding="utf-8"))
        if mine != theirs:
            first = _first_difference(mine, theirs)
            pytest.fail(
                f"{rel} has drifted from app's copy: first difference at "
                f"character {first}\n"
                f"  data: {mine[first:first + 120]!r}\n"
                f"  app:  {theirs[first:first + 120]!r}",
                pytrace=False)


def test_the_shared_runtime_files_are_byte_equal_to_app() -> None:
    """Every shared runtime file is app's file, byte for byte."""
    _skip_or_fail()
    shared = _shared_files()
    assert shared, "no shared runtime file found -- the guard would be blind"
    drifted = []
    for rel in shared:
        theirs = _app_head(rel)
        if theirs is None:
            continue
        mine = _normalised((REPO / rel).read_text(encoding="utf-8"))
        if mine != theirs:
            first = _first_difference(mine, theirs)
            drifted.append(f"{rel} (character {first})")
    assert not drifted, (
        "these shared runtime files differ from app's checkout -- app's is "
        "the base, so copy it byte-for-byte (or write the exception down in "
        "AGENTS.md):\n  " + "\n  ".join(drifted))
