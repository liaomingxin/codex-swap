"""Textual app for ``cxswap tui``.

Everything in this module requires textual (imported at the top) — the lazy
entry point is :mod:`codex_swap.tui`, which imports us only after checking
the dependency is present.
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static

from codex_swap import __version__
from codex_swap.display import fmt_duration, health_color_rich, window_label
from codex_swap.switcher import CodexAccountSwitcher

AUTO_REFRESH_S = 60


def _rich_bar(pct: float, width: int = 26) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    color = health_color_rich(pct)
    return f"[{color}]" + "█" * filled + "[/][dim]" + "░" * (width - filled) + "[/dim]"


def _rich_reset(reset_at: int | None, now: float) -> str:
    if not reset_at:
        return "-"
    remaining = reset_at - now
    if remaining <= 0:
        return "[yellow]resetting[/yellow]"
    absolute = datetime.fromtimestamp(reset_at, tz=UTC).astimezone()
    return f"in {fmt_duration(remaining)} · {absolute:%m-%d %H:%M}"


def _window_row(label: str, window: dict, now: float) -> str:
    pct = float(window.get("usedPercent", 0))
    hit = "  [red]LIMIT REACHED[/red]" if window.get("limitReached") else ""
    return (
        f"  {label:<8} {_rich_bar(pct)}  [{health_color_rich(pct)}]{pct:3.0f}%[/]   "
        f"[dim]{_rich_reset(window.get('resetsAt'), now)}[/dim]{hit}"
    )


def _credits_line(usage: dict) -> str | None:
    parts = []
    balance = usage.get("creditsBalance")
    if usage.get("creditsUnlimited"):
        parts.append("unlimited")
    elif isinstance(balance, str) and balance not in ("", "0"):
        parts.append(f"${balance} balance")
    local = usage.get("approxLocalMessages")
    if isinstance(local, list) and len(local) == 2:
        parts.append(f"~{local[1]} local msgs left" if local[1] else "no local msgs left")
    cloud = usage.get("approxCloudMessages")
    if isinstance(cloud, list) and len(cloud) == 2 and cloud[1]:
        parts.append(f"~{cloud[1]} cloud msgs left")
    avail = usage.get("resetCreditsAvailable")
    if isinstance(avail, int):
        applicable = usage.get("resetCreditsApplicable")
        tag = f"{avail} rate-limit reset{'s' if avail != 1 else ''} available"
        if isinstance(applicable, int) and applicable != avail:
            tag += f" ({applicable} applicable now)"
        parts.append(tag)
    return "  · ".join(parts) if parts else None


def _token_line(row: dict) -> str | None:
    parts = []
    expires = row.get("accessTokenExpiresAt")
    if row.get("accessExpired"):
        parts.append("[red]token EXPIRED[/red]")
    elif expires:
        try:
            expires_dt = datetime.fromisoformat(expires)
            days = (expires_dt - datetime.now(UTC)).total_seconds() / 86400
            parts.append(f"token valid {days:.0f}d")
        except ValueError:
            pass
    last_refresh = row.get("lastRefresh")
    if isinstance(last_refresh, str) and last_refresh[:10]:
        parts.append(f"refreshed {last_refresh[:10]}")
    until = row.get("subscriptionActiveUntil")
    if isinstance(until, str) and until[:10]:
        parts.append(f"subscription until {until[:10]}")
    return "  · ".join(parts) if parts else None


def _stats_line(stats: dict) -> str | None:
    parts = []
    if stats.get("displayName"):
        parts.append(str(stats["displayName"]))
    lt = stats.get("lifetime_tokens")
    if isinstance(lt, (int, float)):
        parts.append(f"lifetime {lt / 1e6:.1f}M tok")
    streak = stats.get("current_streak_days")
    best = stats.get("longest_streak_days")
    if isinstance(streak, int):
        tag = f"streak {streak}d"
        if isinstance(best, int):
            tag += f" (best {best}d)"
        parts.append(tag)
    threads = stats.get("total_threads")
    if isinstance(threads, int):
        parts.append(f"{threads} threads")
    effort = stats.get("most_used_effort")
    if effort:
        pct = stats.get("most_used_effort_pct")
        tag = f"effort: {effort}"
        if isinstance(pct, (int, float)):
            tag += f" {pct:.0f}%"
        parts.append(tag)
    return "  · ".join(parts) if parts else None


class ConfirmSwitchScreen(ModalScreen[int | None]):
    """Yes/no modal before a switch (digits are one keypress away)."""

    CSS = """
    ConfirmSwitchScreen {
        align: center middle;
        background: $background 60%;
    }
    #confirm-box {
        border: round $accent;
        padding: 1 3;
        background: $surface;
    }
    """

    BINDINGS: ClassVar = [
        ("y", "confirm", "Yes, switch"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, target: int, email: str | None) -> None:
        super().__init__()
        self.target = target
        self._email = email

    def compose(self) -> ComposeResult:
        yield Static(
            f"Switch to account {self.target} ({self._email or '?'})?\n"
            "[dim]y = yes · n / esc = no[/dim]",
            id="confirm-box",
        )

    def action_confirm(self) -> None:
        self.dismiss(self.target)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CodexSwapTui(App[None]):
    TITLE = "codex-swap"
    SUB_TITLE = "multi-account switcher for the Codex CLI"

    CSS = """
    Screen { background: $surface; }
    #cards { padding: 0 2; }
    .card {
        border: round $panel;
        padding: 0 1;
        margin-bottom: 1;
        background: $surface;
    }
    .card.-active { border: round $accent; }
    #status {
        dock: bottom;
        height: 1;
        padding: 0 2;
        color: $text-muted;
        background: $panel;
    }
    """

    BINDINGS: ClassVar = [
        Binding("r", "refresh", "Refresh usage"),
        Binding("s", "switch_next", "Switch next"),
        Binding("a", "toggle_auto", "Toggle auto-refresh"),
        Binding("q", "quit", "Quit", priority=True),
        *[Binding(str(n), f"slot('{n}')", show=False) for n in range(1, 10)],
    ]

    def __init__(self, switcher: CodexAccountSwitcher, *args, opener=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._switcher = switcher
        self._opener = opener
        self._auto = True
        self._status_text = ""
        self._last_cards: list[str] = []  # rendered markup, for tests/tools

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="cards"):
            yield Static("[dim]loading usage…[/dim]", id="loading")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.load_usage(force=False)
        self.set_interval(AUTO_REFRESH_S, self._tick_auto)

    # ------------------------------------------------------------- workers

    @work(exclusive=True, thread=True)
    def load_usage(self, force: bool) -> None:
        try:
            report = self._switcher.usage_report(force=force, opener=self._opener)
        except Exception as e:
            self.call_from_thread(self._render_error, str(e))
            return
        self.call_from_thread(self._render, report)

    def action_refresh(self) -> None:
        self.load_usage(force=True)

    def _tick_auto(self) -> None:
        if self._auto:
            self.load_usage(force=False)

    # ------------------------------------------------------------- actions

    def action_toggle_auto(self) -> None:
        self._auto = not self._auto
        state = "ON (60s)" if self._auto else "OFF"
        self._set_status(f"auto-refresh {state}")

    def action_switch_next(self) -> None:
        self._ask_and_switch(None)

    def action_slot(self, slot: str) -> None:
        self._ask_and_switch(slot)

    def _ask_and_switch(self, target: str | None) -> None:
        try:
            entry = self._switcher.resolve_target(target)
        except Exception:
            self._set_status("nothing to switch to")
            return

        def _confirmed(result: int | None) -> None:
            if result is not None:
                self._do_switch(str(result))

        self.push_screen(
            ConfirmSwitchScreen(entry.number, entry.email or entry.account_id),
            _confirmed,
        )

    @work(exclusive=True, thread=True)
    def _do_switch(self, target: str) -> None:
        try:
            result = self._switcher.switch(target)
        except Exception as e:
            self.call_from_thread(self._set_status, f"switch failed: {e}")
            return
        to = result.get("to") or {}
        state = "switched to" if result.get("switched") else "already on"
        self.call_from_thread(
            self._set_status, f"{state} {to.get('number')} ({to.get('email')})"
        )
        self.load_usage(force=False)

    # -------------------------------------------------------------- render

    def _set_status(self, text: str) -> None:
        self._status_text = text
        with contextlib.suppress(Exception):
            self.query_one("#status", Static).update(text)

    def _render_error(self, message: str) -> None:
        self._render(
            {"schemaVersion": 1, "activeAccountNumber": None, "accounts": []},
            error=message,
        )

    def _render(self, report: dict, error: str | None = None) -> None:
        now = time.time()
        stamp = datetime.now().strftime("%H:%M:%S")
        self.sub_title = (
            f"v{__version__} · auto {'60s' if self._auto else 'off'} · updated {stamp}"
        )
        cards = self.query_one("#cards", VerticalScroll)
        cards.remove_children()
        self._last_cards = []

        if error:
            cards.mount(Static(f"[red]usage fetch failed:[/red] {error}", classes="card"))
            return
        rows = report["accounts"]
        if not rows:
            self._last_cards = []
            cards.mount(
                Static(
                    "No accounts stored. Run [b]codex login[/b], then [b]cxswap add[/b].",
                    classes="card",
                )
            )
            return

        for row in rows:
            markup = self._card_markup(row, now)
            self._last_cards.append(markup)
            cards.mount(Static(markup, classes=self._card_classes(row)))
        keys = "r refresh · s next · 1-9 switch · a auto · q quit"
        if self._status_text:
            self._set_status(f"{self._status_text}  ·  updated {stamp}  ·  {keys}")
        else:
            self._set_status(f"updated {stamp}  ·  {keys}")

    @staticmethod
    def _card_classes(row: dict) -> str:
        return "card" + (" -active" if row.get("active") else "")

    @staticmethod
    def _card_markup(row: dict, now: float) -> str:
        usage = row.get("usage") or {}
        number = row["number"]
        email = (row.get("email") or "?")[:38]
        plan = usage.get("planType") or row.get("plan") or "-"

        if row.get("active"):
            lines = [
                f"[bold cyan]● {number}[/] [bold]{email}[/] "
                f"[dim]· {plan}[/]  [cyan]◀ active[/]"
            ]
        else:
            lines = [f"[dim]○[/] {number}  {email} [dim]· {plan}[/]"]

        if not usage:
            status = row.get("usageStatus", "ok")
            note = {
                "auth-needed": "token expired — switch here and run codex once, then re-add",
                "error": row.get("usageError", "unavailable"),
            }.get(status, "no data yet")
            lines.append(f"  [yellow]{note}[/yellow]")
            return "\n".join(lines)

        primary = usage.get("primaryWindow")
        secondary = usage.get("secondaryWindow")
        for w in (primary, secondary):
            if w:
                label = window_label(w.get("windowSeconds", 0))
                lines.append(_window_row(label, w, now))
        for m in usage.get("modelLimits", []):
            parts = []
            for wkey in ("primaryWindow", "secondaryWindow"):
                w = m.get(wkey)
                if not w:
                    continue
                pct = float(w.get("usedPercent", 0))
                parts.append(
                    f"{window_label(w.get('windowSeconds', 0))} "
                    f"[{health_color_rich(pct)}]{pct:.0f}%[/] "
                    f"[dim]→ {_rich_reset(w.get('resetsAt'), now)}[/dim]"
                )
            if parts:
                lines.append(
                    f"  [cyan]·[/] {(m.get('name') or '?')[:28]:<30} {'   '.join(parts)}"
                )
        credits = _credits_line(usage)
        if credits:
            lines.append(f"  [dim]{credits}[/dim]")
        if row.get("stats"):
            stats_line = _stats_line(row["stats"])
            if stats_line:
                lines.append(f"  [dim]{stats_line}[/dim]")
        token_line = _token_line(row)
        if token_line:
            lines.append(f"  [dim]{token_line}[/dim]")
        return "\n".join(lines)
