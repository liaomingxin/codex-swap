"""Atomic, durable file operations.

``auth.json`` is the single source of truth for the active Codex login: a
torn write (crash mid-write, full disk) would log the user out of *every*
account at once, because the refresh token lives only in that file. So every
write goes through temp-file-in-same-dir -> fsync -> ``os.replace`` (atomic
on POSIX and Windows) -> fsync of the directory entry.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory entry (POSIX only, harmless no-op elsewhere)."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(directory, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict | None:
    """Read a JSON object, returning None when absent or corrupt.

    The account store is advisory state, not credentials: a corrupt
    sequence.json should degrade to "no accounts", never crash the CLI.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
