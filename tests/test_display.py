"""Display helpers: bars, durations, color gating."""

from __future__ import annotations

from codex_swap import display


def setup_function(_):
    display.reset_color_state()


def test_bar_proportions():
    bar = display.bar(50, width=10)
    assert bar.count("█") == 5 and bar.count("░") == 5


def test_bar_clamps_out_of_range():
    assert display.bar(150, width=10).count("█") == 10
    assert display.bar(-5, width=10).count("░") == 10


def test_color_disabled_by_default_when_piped(monkeypatch):
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    display.reset_color_state()
    # pytest captures stdout -> not a tty -> no escapes
    assert "\x1b" not in display.bar(50, width=10)
    assert "\x1b" not in display.bold("x")


def test_no_color_env_wins(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    display.reset_color_state()
    assert "\x1b" not in display.bold("x")


def test_force_color_env(monkeypatch):
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    display.reset_color_state()
    assert "\x1b[1m" in display.bold("x")


def test_fmt_duration_shapes():
    assert display.fmt_duration(45) == "45s"
    assert display.fmt_duration(5 * 60) == "5m"
    assert display.fmt_duration(3 * 3600 + 12 * 60) == "3h12m"
    assert display.fmt_duration(4 * 86400 + 23 * 3600) == "4d23h"
    assert display.fmt_duration(-10) == "0s"


def test_fmt_reset_combines_relative_and_absolute():
    now = 1_000_000.0
    text = display.fmt_reset(now + 90, now=now)
    assert text.startswith("in 1m") and "·" in text
    assert display.fmt_reset(None) == "-"


def test_window_labels():
    assert display.window_label(604800) == "Weekly"
    assert display.window_label(18000) == "5-hour"
    assert display.window_label(3600) == "1h"
    assert display.window_label(3 * 86400) == "3.0d"
