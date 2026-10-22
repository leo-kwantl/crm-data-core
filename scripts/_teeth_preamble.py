"""One line before the first mutation: a probe that dies must say why.

A probe that dies with ZERO bytes on stdout leaves no artefact to
reconcile against, and the failure that killed it is invisible -- the
manager hit exactly that on 2026-10-22: a teeth run exited 1 with no
output at all, because the script's first print was per-mutation and
the death came before the first one.

So every mutation-check script announces itself BEFORE it mutates
anything: what it is about to do, the HEAD it measures against, the tree
state it accepted, and the pin that must fail. Then a death is a
sentence, not a zero-byte file.

    from _teeth_preamble import announce
    announce("the shared statement's serving form", ROOT, TEST)
"""
from __future__ import annotations

import pathlib
import subprocess


def _git(root: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)
    return proc.stdout.strip()


def announce(what: str, root: pathlib.Path, pin: str,
             targets: list[str] | None = None) -> None:
    """Print the one line that makes a dying probe legible."""
    head = _git(root, "rev-parse", "--short", "HEAD")
    status = [line for line in _git(root, "status", "--porcelain").splitlines()
              if line.strip()]
    tree = "clean" if not status else f"dirty({len(status)} entries)"
    detail = ""
    if status:
        detail = " [" + " | ".join(status[:3]) + "]"
    target = ""
    if targets:
        target = " | targets " + ",".join(targets)
    print(f"[START] {what} | HEAD {head} | tree {tree}{detail}"
          f" | pin {pin}{target}")
