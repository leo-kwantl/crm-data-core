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

import os
import pathlib
import subprocess
import time


def _git(root: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)
    return proc.stdout.strip()


def commit_bytes(root: pathlib.Path, rel: str) -> bytes | None:
    """The file's bytes as COMMITTED, or None when it is not in HEAD."""
    proc = subprocess.run(["git", "cat-file", "blob", f"HEAD:{rel}"],
                          cwd=root, capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def bytes_differ_from_commit(root: pathlib.Path, rel: str) -> bool:
    """True when the WORKING COPY's BYTES differ from the commit's.

    `git diff` is not this question: with `text=auto` it normalises the
    line endings before comparing, so a CRLF working copy reads as
    unchanged while every byte differs from the blob. A teeth run
    restores BYTES, so the baseline it has to know is the committed
    BYTES -- and a checkout whose line endings differ from the commit
    is exactly the case where the baseline is unknown.
    """
    committed = commit_bytes(root, rel)
    if committed is None:
        return True
    path = root / rel
    if not path.exists():
        return True
    return path.read_bytes() != committed


def require_byte_baseline(root: pathlib.Path, rels: list[str]) -> bool:
    """Refuse when a file the run mutates does not match the commit's bytes.

    The refusal is right: the baseline is unknown. The MESSAGE says what
    is actually compared -- the BYTES, not the tree's cleanliness.
    """
    stale = [rel for rel in rels if bytes_differ_from_commit(root, rel)]
    if not stale:
        return True
    print("[REFUSED] these files' bytes differ from the commit (the "
          "checkout's line endings count), so the baseline is unknown:")
    for rel in stale:
        print("        ", rel)
    print("         fix: check the file out with the commit's bytes "
          "(a clone made with core.autocrlf=false), or commit the change "
          "you are measuring")
    return False


def read_target(path: pathlib.Path) -> tuple[str, bytes]:
    """The target's text (universal newlines) AND its original bytes.

    The text is what the mutations match against; the bytes are what
    the restore must write back. A text round-trip alone is not a
    restore: on Windows `write_text` rewrites LF as CRLF, so the file
    stops matching the commit while the receipt still says restored.
    """
    raw = path.read_bytes()
    return raw.decode("utf-8").replace("\r\n", "\n"), raw


def write_mutation(path: pathlib.Path, text: str, original: bytes) -> None:
    """Write a mutated text in the file's OWN newline style."""
    body = text.replace("\n", "\r\n") if b"\r\n" in original else text
    path.write_bytes(body.encode("utf-8"))


def pid_alive(pid: int) -> bool:
    """True when that process is still running (Windows-safe).

    `os.kill(pid, 0)` is not the question on Windows -- there it posts a
    console control event to the process instead of probing it, which
    would signal a live run. So the probe is the kernel's: open the
    process and read its exit code.
    """
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(lock: pathlib.Path, stale_seconds: int = 15 * 60) -> None:
    """Take the run's lock, or refuse the run.

    A lock whose OWNER IS DEAD is not a lock: the run that wrote it was
    killed, and a killed run leaves its mutation in the tree (the
    restore lives in a `finally`, which a kill never reaches). So the
    pid in the lock is probed for liveness, and a dead owner is taken
    over with a line saying so -- instead of blocking the next run for
    the whole stale window, which is what a killed run used to do.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        owner: int | None
        try:
            owner = int(lock.read_text().strip() or "0")
        except (ValueError, OSError):
            owner = None
        if owner and not pid_alive(owner):
            print(f"[LOCK ] taking over a lock whose owner (pid {owner}) is "
                  f"dead - that run was killed, and its mutation may still "
                  f"be in the tree")
            lock.unlink(missing_ok=True)
        elif age < stale_seconds:
            raise SystemExit(
                f"[REFUSED] another run holds {lock.name} ({int(age)}s old, "
                f"pid {owner}) - two runs would interleave their mutations")
        else:
            print(f"[LOCK ] taking over a stale lock ({int(age)}s old)")
            lock.unlink(missing_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def announce(what: str, root: pathlib.Path, pin: str,
             targets: list[str] | None = None) -> None:
    """Print the one line that makes a dying probe legible.

    The tree state it reports is `git status`'s, which is a NORMALISED
    view: with `text=auto` git compares line-ending-insensitively, so a
    CRLF working copy reads "clean" while every byte differs from the
    commit. So the line also says, for the files the run mutates, whether
    their BYTES match the commit -- that is the baseline the run needs.
    """
    head = _git(root, "rev-parse", "--short", "HEAD")
    status = [line for line in _git(root, "status", "--porcelain").splitlines()
              if line.strip()]
    tree = "clean" if not status else f"dirty({len(status)} entries)"
    detail = ""
    if status:
        detail = " [" + " | ".join(status[:3]) + "]"
    target = ""
    if targets:
        drifted = [rel for rel in targets if bytes_differ_from_commit(root, rel)]
        state = "bytes match the commit" if not drifted else (
            "BYTES DIFFER: " + ",".join(drifted))
        target = f" | targets {','.join(targets)} ({state})"
    print(f"[START] {what} | HEAD {head} | tree {tree}{detail}"
          f" | pin {pin}{target}")
