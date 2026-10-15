"""Tick-time DB resolution for scripts/run_pipeline.py (pure tests).

The runner must land on the LIVE postmaster at every tick: pgembed
picks a random port per restart, so ports are re-read from pidfiles,
never pinned — and a missing postmaster fails loudly instead of
booting a silent empty DB.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _load():
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location(
        "run_pipeline_under_test", REPO / "scripts" / "run_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_rp = _load()


def test_pidfile_port_valid(tmp_path: Path):
    d = tmp_path / ".pgembed-data"
    d.mkdir()
    (d / "postmaster.pid").write_text(
        "4592\nC:/x/.pgembed-data\n1791985484\n61983\n", encoding="utf-8")
    assert _rp._pidfile_port(d) == 61983


def test_pidfile_port_absent_or_garbage(tmp_path: Path):
    assert _rp._pidfile_port(tmp_path / "nope") is None
    d = tmp_path / "bad"
    d.mkdir()
    (d / "postmaster.pid").write_text("garbage\n", encoding="utf-8")
    assert _rp._pidfile_port(d) is None


def test_candidates_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
    d = tmp_path / ".pgembed-data"
    d.mkdir()
    (d / "postmaster.pid").write_text(
        "1\nx\n2\n54321\n", encoding="utf-8")
    cands = _rp._db_url_candidates("postgresql://cli/db")
    assert cands[0] == "postgresql://cli/db"
    assert cands[1] == "postgresql://env/db"
    assert "postgresql://postgres@127.0.0.1:54321/postgres" in cands
    # no CLI, no env -> pidfile-derived only (never an invented
    # default). Other entries may appear on dev boxes with a live
    # app checkout — assert presence, not exclusivity.
    monkeypatch.delenv("DATABASE_URL")
    cands2 = _rp._db_url_candidates(None)
    assert "postgresql://postgres@127.0.0.1:54321/postgres" in cands2
