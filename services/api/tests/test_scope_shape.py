"""The scope vocabulary data's CLIs accept (A114; manager root `e4a8e4f`).

The rule: a scope is the pair `(brand_id, group_id)`; a group scope is only
ever the pair, and the pair is not selectable today -- so no job may stamp
a group scope, and the CLIs refuse one at their own boundary, before the
connection.

Each CLI surface is checked as a PROCESS, against a DSN nothing can
connect to: a CLI that reaches the cluster fails with a connection error, so
a refusal is proof that the guard runs before the connection and not after
it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from pipelines.scope_shape import (  # noqa: E402
    UnselectableScopeRefused, require_selectable_scope)

UNREACHABLE = "postgresql://nobody@127.0.0.1:1/nothing"
GROUP = "11111111-1111-1111-1111-111111111111"
BRAND = "22222222-2222-2222-2222-222222222222"


def _cli(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = UNREACHABLE
    return subprocess.run([sys.executable, *args], cwd=REPO,
                          capture_output=True, text=True, env=env, timeout=180)


def _assert_refused(proc: subprocess.CompletedProcess, shape: str) -> None:
    assert proc.returncode == 2, f"exit {proc.returncode}: {proc.stderr}"
    assert f"refused the scope {shape}" in proc.stderr, proc.stderr
    assert "no reader can select" in proc.stderr, proc.stderr
    # The guard runs BEFORE the connection: a DSN that cannot be reached
    # would say so, and the refusal would not be there at all.
    assert "connection" not in proc.stderr.lower(), proc.stderr


def test_a_brand_scope_and_the_global_scope_are_legal() -> None:
    assert require_selectable_scope(BRAND, None) is None
    assert require_selectable_scope(None, None) is None
    assert require_selectable_scope(None, "") is None


def test_the_group_alone_is_refused_by_name() -> None:
    with pytest.raises(UnselectableScopeRefused) as exc:
        require_selectable_scope(None, GROUP)
    message = str(exc.value)
    assert "(None, group_id)" in message
    assert "no reader can select" in message


def test_the_pair_is_refused_by_name() -> None:
    with pytest.raises(UnselectableScopeRefused) as exc:
        require_selectable_scope(BRAND, GROUP)
    message = str(exc.value)
    assert "(brand_id, group_id)" in message
    assert "no reader can select" in message


def test_run_pipeline_refuses_a_group_scope() -> None:
    _assert_refused(
        _cli(["scripts/run_pipeline.py", "--pipeline", "nightly",
              "--group-id", GROUP]),
        "(None, group_id)")


def test_run_daily_snapshot_refuses_a_group_scope() -> None:
    _assert_refused(
        _cli(["scripts/run_daily_snapshot.py", "--group-id", GROUP]),
        "(None, group_id)")


def test_run_upload_refuses_a_group_scope() -> None:
    _assert_refused(
        _cli(["scripts/run_upload.py", "orders", "no-such-file.csv",
              "--group-id", GROUP]),
        "(None, group_id)")


def test_run_pipeline_refuses_the_pair() -> None:
    _assert_refused(
        _cli(["scripts/run_pipeline.py", "--pipeline", "nightly",
              "--brand-id", BRAND, "--group-id", GROUP]),
        "(brand_id, group_id)")
