"""Path resolution: where Codex lives and where codex-swap keeps its data.

Honours two environment variables so tests (and a future ``cxswap run``
session mode) never touch real state:

- ``CODEX_HOME``      — same variable the Codex CLI itself honours; the auth
                        file is ``$CODEX_HOME/auth.json`` (default ``~/.codex``).
- ``CODEX_SWAP_BACKUP`` — root for our account store
                        (default ``~/.codex-swap-backup``).

Codex stores credentials in a plain file on every platform (verified:
``codex doctor`` reports "auth storage mode: File"), so there is no
Keychain/registry branching here — unlike claude-swap.
"""

from __future__ import annotations

import os
from pathlib import Path

AUTH_FILENAME = "auth.json"
SEQUENCE_FILENAME = "sequence.json"
CREDENTIALS_DIRNAME = "credentials"
UNCLAIMED_DIRNAME = "unclaimed"


def codex_home() -> Path:
    """The active Codex config home (CODEX_HOME or ~/.codex)."""
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def auth_path() -> Path:
    return codex_home() / AUTH_FILENAME


def backup_root() -> Path:
    env = os.environ.get("CODEX_SWAP_BACKUP")
    return Path(env).expanduser() if env else Path.home() / ".codex-swap-backup"


def sequence_path() -> Path:
    return backup_root() / SEQUENCE_FILENAME


def credentials_dir() -> Path:
    return backup_root() / CREDENTIALS_DIRNAME


def unclaimed_dir() -> Path:
    return backup_root() / UNCLAIMED_DIRNAME
