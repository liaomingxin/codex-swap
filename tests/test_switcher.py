"""End-to-end switcher behaviour against an isolated CODEX_HOME."""

from __future__ import annotations

import json
import time

from codex_swap.paths import auth_path
from codex_swap.switcher import CodexAccountSwitcher
from codex_swap.exceptions import AuthFileError

from conftest import make_auth_json


def _login(email: str, account_id: str, refresh: str | None = None) -> None:
    auth_path().write_text(
        make_auth_json(email=email, account_id=account_id, refresh_token=refresh or f"rt.1.{email}"),
        encoding="utf-8",
    )


def test_add_then_status_and_list(live_auth):
    sw = CodexAccountSwitcher()
    entry = sw.add()
    assert entry.number == 1 and entry.email == "user@example.com"
    payload = sw.list_payload()
    assert payload["activeAccountNumber"] == 1
    assert payload["accounts"][0]["active"] is True
    assert payload["accounts"][0]["plan"] == "pro"


def test_add_rejects_missing_auth(isolated_homes):
    try:
        CodexAccountSwitcher().add()
        raise AssertionError("expected AuthFileError")
    except AuthFileError:
        pass


def test_add_rejects_garbage_auth(isolated_homes):
    auth_path().write_text("this is not json", encoding="utf-8")
    try:
        CodexAccountSwitcher().add()
        raise AssertionError("expected AuthFileError")
    except AuthFileError:
        pass


def test_switch_swaps_file_bytes(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()
    original = auth_path().read_text()

    _login("second@x.io", "acc-second")
    sw.add()

    result = sw.switch("1")
    assert result["switched"] is True
    assert auth_path().read_text() == original

    result = sw.switch("2")
    assert result["switched"] is True
    assert json.loads(auth_path().read_text())["tokens"]["account_id"] == "acc-second"


def test_switch_is_noop_for_active_account(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()
    before = auth_path().read_text()
    result = sw.switch("1")
    assert result["switched"] is False
    assert auth_path().read_text() == before


def test_rotation_wraps(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()  # slot 1 active
    _login("second@x.io", "acc-second")
    sw.add()  # slot 2 active
    assert sw.switch()["to"]["number"] == 1  # wraps to smallest
    assert sw.switch()["to"]["number"] == 2  # then forward again


def test_switch_away_stashes_refreshed_tokens(live_auth):
    """The core safety property: a newer live token is never lost on switch."""
    sw = CodexAccountSwitcher()
    sw.add()                                  # slot 1: user@example.com
    _login("second@x.io", "acc-second")
    sw.add()                                  # slot 2: second@x.io
    sw.switch("1")                            # slot 1 now live (original bytes)

    # simulate codex refreshing the live login while it is active
    _login("user@example.com", "acc-1234", refresh="rt.1.ROTATED-LIVE")
    rotated_live = auth_path().read_text()

    result = sw.switch("2")                    # switch away: slot 1 must capture the rotation
    assert result["switched"] is True
    entries = {e.number: e for e in sw.store.list_entries()}
    assert sw.store.read_credential(entries[1]) == rotated_live

    # ...and switching back installs the rotated bytes, not the stale snapshot
    sw.switch("1")
    assert auth_path().read_text() == rotated_live


def test_switch_over_unmanaged_login_parks_it(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()
    _login("stranger@x.io", "acc-stranger")  # never `add`ed
    result = sw.switch("1")
    assert result["switched"] is True
    assert result["stashed"] is not None
    rows = sw.list_unclaimed()
    assert len(rows) == 1 and rows[0]["email"] == "stranger@x.io"


def test_switch_over_garbage_login_overwrites(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()
    auth_path().write_text("garbage", encoding="utf-8")
    result = sw.switch("1")
    assert result["switched"] is True
    assert json.loads(auth_path().read_text())["tokens"]["account_id"] == "acc-1234"


def test_switch_with_no_accounts_raises(live_auth):
    try:
        CodexAccountSwitcher().switch()
        raise AssertionError("expected SwitchError")
    except Exception as e:
        assert "No accounts" in str(e)


def test_expired_access_token_flagged_in_list(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()
    _login("old@x.io", "acc-old")
    auth_path().write_text(
        make_auth_json(email="old@x.io", account_id="acc-old", access_exp=time.time() - 3600),
        encoding="utf-8",
    )
    sw.add()
    payload = sw.list_payload()
    row = next(r for r in payload["accounts"] if r["email"] == "old@x.io")
    assert row["accessExpired"] is True


def test_remove_leaves_live_login_alone(live_auth):
    sw = CodexAccountSwitcher()
    sw.add()
    before = auth_path().read_text()
    sw.remove(1)
    assert auth_path().read_text() == before
