"""Lazy entry point for ``cxswap tui``.

Importing this module must stay cheap and dependency-free; textual is only
touched inside :func:`run`, so the CLI can print an actionable install hint
instead of a traceback when the ``tui`` extra is missing.
"""

from __future__ import annotations

from codex_swap.exceptions import CodexSwapError


def run() -> None:
    try:
        from textual.app import App  # noqa: F401
    except ImportError as e:
        raise CodexSwapError(
            "the TUI needs the 'textual' package: "
            "uv tool install --editable '.[tui]'  "
            "(or run via: uv run --with textual cxswap tui)"
        ) from e

    from codex_swap.switcher import CodexAccountSwitcher
    from codex_swap.tui_app import CodexSwapTui

    CodexSwapTui(CodexAccountSwitcher()).run()
