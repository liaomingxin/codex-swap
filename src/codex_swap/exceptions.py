"""Exception hierarchy for codex-swap.

Every user-facing failure is a CodexSwapError subclass so the CLI can render
``Error: <message>`` uniformly and scripts get a stable exit code (1).
"""

from __future__ import annotations


class CodexSwapError(Exception):
    """Base class for all codex-swap errors."""


class AuthFileError(CodexSwapError):
    """~/.codex/auth.json is missing, unreadable, or not valid JSON."""


class StoreError(CodexSwapError):
    """The backup store (sequence.json / credential files) is inconsistent."""


class SwitchError(CodexSwapError):
    """A switch could not be completed."""


class AccountNotFoundError(CodexSwapError):
    """No account matches the given number/email."""
