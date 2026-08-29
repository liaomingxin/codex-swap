"""TUI tests via Textual's pilot harness (fully offline, seeded cache)."""

from __future__ import annotations

import pytest
from conftest import make_auth_json

from codex_swap.paths import auth_path
from codex_swap.switcher import CodexAccountSwitcher
from codex_swap.usage_store import UsageCache

pytest.importorskip("textual")


class NoNet:
    def __call__(self, req, timeout=None):
        raise AssertionError("TUI tests must serve from the cache, not the network")


def _seed_two_accounts() -> CodexAccountSwitcher:
    sw = CodexAccountSwitcher()
    auth_path().write_text(
        make_auth_json(email="a@x.io", account_id="acc-a", refresh_token="rt.1.a")
    )
    sw.add()
    auth_path().write_text(
        make_auth_json(email="b@x.io", account_id="acc-b", refresh_token="rt.1.b")
    )
    sw.add()
    cache = UsageCache()
    now = __import__("time").time()
    from codex_swap.usage import UsageSnapshot, UsageWindow

    def snap(used: float) -> UsageSnapshot:
        return UsageSnapshot(
            email="x@x.io",
            account_id="acc",
            plan_type="pro",
            primary=UsageWindow(used, 604800, int(now) + 86400),
            secondary=None,
            fetched_at=now,
        )

    cache.put(1, snap(42))
    cache.put(2, snap(7))
    return sw


@pytest.mark.asyncio
async def test_tui_renders_account_cards():
    from codex_swap.tui_app import CodexSwapTui

    sw = _seed_two_accounts()
    app = CodexSwapTui(sw, opener=NoNet())
    async with app.run_test() as pilot:
        await pilot.pause()
        joined = "\n".join(app._last_cards)
        assert "a@x.io" in joined and "b@x.io" in joined
        assert "◀ active" in joined  # slot 2 is the live login


@pytest.mark.asyncio
async def test_tui_switch_via_key_and_confirm():
    from codex_swap.tui_app import CodexSwapTui

    sw = _seed_two_accounts()
    app = CodexSwapTui(sw, opener=NoNet())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("1")  # ask to switch to slot 1
        await pilot.pause()
        # the confirm modal should be up; confirm with y
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()
        assert json_loads_safe(auth_path())["tokens"]["account_id"] == "acc-a", (
            "switch must have rewritten auth.json to slot 1's account"
        )


def json_loads_safe(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_tui_toggle_auto_and_quit_binding_exist():
    from codex_swap.tui_app import CodexSwapTui

    sw = _seed_two_accounts()
    app = CodexSwapTui(sw, opener=NoNet())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        assert app._auto is False
        await pilot.press("a")
        assert app._auto is True
