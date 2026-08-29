"""Human-friendly rendering: color, bars, durations — no dependencies.

Color is TTY-gated (and honours ``NO_COLOR``): the same string builders
produce plain text when piped, so ``cxswap usage | grep ...`` never carries
escape codes. All functions taking times accept an injectable ``now`` so
tests are deterministic.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime

# ------------------------------------------------------------------ color

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"


def _stream_supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE") or os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


_COLOR_STATE: bool | None = None


def use_color(stream=None) -> bool:
    """Resolved once, then cached — paint() consults the cache so every
    rendered string obeys the same decision (pipes and NO_COLOR stay clean,
    CLICOLOR_FORCE can turn color on for captured screenshots)."""
    global _COLOR_STATE
    if _COLOR_STATE is None:
        _COLOR_STATE = _stream_supports_color(stream if stream is not None else sys.stdout)
    return _COLOR_STATE


def reset_color_state() -> None:
    """Test hook: re-resolve color on next use."""
    global _COLOR_STATE
    _COLOR_STATE = None


def paint(text: str, *codes: str) -> str:
    if not codes or not use_color():
        return text
    return "".join(codes) + text + _RESET


def bold(text: str) -> str:
    return paint(text, _BOLD)


def dim(text: str) -> str:
    return paint(text, _DIM)


def health_color(pct: float) -> str:
    """Threshold-aware severity: comfortable / warming / at-the-line."""
    if pct >= 80:
        return _RED
    if pct >= 50:
        return _YELLOW
    return _GREEN


# ------------------------------------------------------------------- bars

BAR_WIDTH = 26


def bar(pct: float, width: int = BAR_WIDTH, color: str | None = None) -> str:
    """A unicode usage bar; ``color`` defaults to the health color."""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    color = color or health_color(pct)
    return paint("█" * filled, color) + paint("░" * (width - filled), _DIM)


# --------------------------------------------------------------- durations


def fmt_duration(seconds: float) -> str:
    """Compact human duration: 4d23h, 5h12m, 45m, 12s."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{hours // 24}d{hours % 24}h"


def fmt_reset(reset_at: int | float | None, now: float | None = None) -> str:
    """Relative + absolute reset time, e.g. ``in 4d23h · 09-03 20:35``."""
    if not reset_at:
        return "-"
    now = time.time() if now is None else now
    remaining = reset_at - now
    if remaining <= 0:
        return paint("resetting", _YELLOW)
    absolute = datetime.fromtimestamp(reset_at, tz=UTC).astimezone()
    return f"in {fmt_duration(remaining)} · {absolute:%m-%d %H:%M}"


def fmt_age(seconds: float) -> str:
    return fmt_duration(seconds) + " ago"


# ------------------------------------------------------------------ labels


def window_label(window_seconds: int | float) -> str:
    """Human name for a window span (Weekly / 5-hour / <n>h)."""
    hours = window_seconds / 3600
    if hours >= 96:
        return "Weekly"
    if 4 <= hours <= 6:
        return "5-hour"
    if hours >= 24:
        return f"{hours / 24:.1f}d"
    return f"{hours:.0f}h"
