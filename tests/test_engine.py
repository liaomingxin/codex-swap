"""Auto-switch engine policy: threshold, hysteresis, cooldown, strategies."""

from __future__ import annotations

import contextlib
import json
import time

from conftest import make_auth_json

from codex_swap.engine import AutoConfig, AutoSwitchEngine, _binding_pct
from codex_swap.paths import auth_path, backup_root
from codex_swap.switcher import CodexAccountSwitcher
from codex_swap.usage import ModelLimit, UsageSnapshot, UsageWindow
from codex_swap.usage_store import UsageCache


def _wind(used: float, window_s: int = 604800) -> dict:
    """JSON-shaped window (as usage_report rows carry it)."""
    return {
        "usedPercent": used,
        "windowSeconds": window_s,
        "resetsAt": int(time.time()) + 3600,
    }


def _win(used: float, window_s: int = 604800) -> UsageWindow:
    return UsageWindow(
        used_percent=used, window_seconds=window_s, reset_at=int(time.time()) + 3600
    )


def _snapshot(
    primary_used: float,
    *,
    secondary: float | None = None,
    model_limits: tuple[ModelLimit, ...] = (),
    limit_reached: bool = False,
) -> UsageSnapshot:
    return UsageSnapshot(
        email="x@x.io",
        account_id="acc",
        plan_type="pro",
        primary=None if primary_used < 0 else _win(primary_used),
        secondary=None if secondary is None else _win(secondary, 18000),
        model_limits=model_limits,
        limit_reached=limit_reached,
        fetched_at=time.time(),
    )


def _setup_two_accounts() -> CodexAccountSwitcher:
    sw = CodexAccountSwitcher()
    auth_path().write_text(
        make_auth_json(email="a@x.io", account_id="acc-a", refresh_token="rt.1.a")
    )
    sw.add()
    auth_path().write_text(
        make_auth_json(email="b@x.io", account_id="acc-b", refresh_token="rt.1.b")
    )
    sw.add()
    return sw  # slot 2 (b@x.io) is live/active


def _seed_cache(cache: UsageCache, active_used: float, other_used: float, **kw):
    cache.put(2, _snapshot(active_used, **kw))  # slot 2 active
    cache.put(1, _snapshot(other_used))
    # make entries fresh so usage_report serves them without network
    return cache


class NoNetOpener:
    def __call__(self, req, timeout=None):
        raise AssertionError("engine tick must serve from cache, not the network")


# ------------------------------------------------------------- binding pct


def test_binding_pct_takes_max_window():
    row = {
        "usage": {
            "primaryWindow": _wind(40),
            "secondaryWindow": None,
            "modelLimits": [
                {
                    "name": "spark",
                    "primaryWindow": _wind(90, 18000),
                    "secondaryWindow": _wind(10),
                },
            ],
        }
    }
    assert _binding_pct(row) == 90


def test_binding_pct_missing_usage():
    assert _binding_pct({"usage": None}) is None
    assert _binding_pct({}) is None


# ----------------------------------------------------------------- policy


def test_below_threshold_stays(live_auth):
    sw = _setup_two_accounts()
    _seed_cache(UsageCache(), active_used=42, other_used=10)
    events = []
    engine = AutoSwitchEngine(sw, on_event=events.append, opener=NoNetOpener())
    ev = engine.tick()
    assert ev["event"] == "no-switch" and ev["reason"] == "below-threshold"
    assert json.loads(auth_path().read_text())["tokens"]["account_id"] == "acc-b"


def test_over_threshold_switches_to_best(live_auth):
    sw = _setup_two_accounts()
    _seed_cache(UsageCache(), active_used=95, other_used=12)
    engine = AutoSwitchEngine(sw, opener=NoNetOpener())
    ev = engine.tick()
    assert ev["event"] == "switch" and ev["to"] == 1 and ev["from"] == 2
    assert json.loads(auth_path().read_text())["tokens"]["account_id"] == "acc-a"
    # outgoing slot re-stashed: switching away captured the live credential
    stored = json.loads(sw.store.read_credential(sw.store.find(2)))
    assert stored["tokens"]["account_id"] == "acc-b"
    # state persisted for cross-process cooldown
    state = json.loads((backup_root() / "autoswitch_state.json").read_text())
    assert state["lastSwitchTo"] == 1 and state["lastSwitchFrom"] == 2


def test_hysteresis_blocks_marginal_candidate(live_auth):
    sw = _setup_two_accounts()
    # candidate at 76% clears neither threshold(80) - hysteresis(5) = 75? 76 >= 75 -> blocked
    _seed_cache(UsageCache(), active_used=85, other_used=76)
    engine = AutoSwitchEngine(sw, opener=NoNetOpener())
    ev = engine.tick()
    assert ev["event"] == "no-switch" and ev["reason"] == "no-viable-candidate"


def test_cooldown_blocks_then_expires(live_auth):
    sw = _setup_two_accounts()
    cache = UsageCache()
    clock = {"now": 1000.0}

    engine = AutoSwitchEngine(
        sw,
        AutoConfig(cooldown_s=300),
        opener=NoNetOpener(),
        now=lambda: clock["now"],
    )
    _seed_cache(cache, active_used=95, other_used=10)
    assert engine.tick()["event"] == "switch"  # t=1000 switch 2 -> 1
    _seed_cache(cache, active_used=95, other_used=10)  # (slot roles swap: 1 now active)
    cache.put(1, _snapshot(95))
    cache.put(2, _snapshot(10))

    clock["now"] = 1100.0
    ev = engine.tick()
    assert ev["event"] == "no-switch" and ev["reason"] == "cooldown"
    assert ev["cooldownRemainingS"] == 200

    clock["now"] = 1400.0  # cooldown elapsed
    ev = engine.tick()
    assert ev["event"] == "switch" and ev["to"] == 2


def test_hard_limit_bypasses_cooldown(live_auth):
    sw = _setup_two_accounts()
    cache = UsageCache()
    clock = {"now": 1000.0}
    engine = AutoSwitchEngine(
        sw, AutoConfig(cooldown_s=300), opener=NoNetOpener(), now=lambda: clock["now"]
    )
    _seed_cache(cache, active_used=95, other_used=10)
    engine.tick()  # switch at t=1000
    cache.put(1, _snapshot(95, limit_reached=True))  # now slot 1 hard at limit
    cache.put(2, _snapshot(10))
    clock["now"] = 1100.0  # still inside cooldown
    ev = engine.tick()
    assert ev["event"] == "switch"  # bypassed: limitReached


def test_strategy_next_rotates_in_order(live_auth):
    sw = _setup_two_accounts()
    # add a third account
    auth_path().write_text(
        make_auth_json(email="c@x.io", account_id="acc-c", refresh_token="rt.1.c")
    )
    sw.add()
    cache = UsageCache()
    _seed_cache(cache, active_used=95, other_used=50)
    cache.put(3, _snapshot(5))  # slot 3 much better than 2

    engine = AutoSwitchEngine(sw, AutoConfig(strategy="next"), opener=NoNetOpener())
    ev = engine.tick()
    # active is 2 -> "next" picks 3 (next number), not the best-by-quota... 3 IS next.
    # Make the discriminator sharp: slot 1 is best (5%), slot 3 is next-in-rotation (30%)
    cache.put(1, _snapshot(5))
    cache.put(3, _snapshot(30))
    auth_path().write_text(
        make_auth_json(email="b@x.io", account_id="acc-b", refresh_token="rt.1.b")
    )
    # (live is slot 2 again; no state written by us)
    ev = engine.tick()
    assert ev["event"] == "switch" and ev["to"] == 3  # rotation order, not best


def test_auth_needed_candidate_skipped(live_auth):
    sw = _setup_two_accounts()
    cache = UsageCache()
    cache.put(2, _snapshot(95))  # only the active slot has data
    entry = sw.store.find(1)
    entry.credential_path().unlink()  # slot 1: no cache, no credential -> error row
    ev = AutoSwitchEngine(sw, opener=NoNetOpener()).tick()
    assert ev["event"] == "no-switch" and ev["reason"] in (
        "no-viable-candidate",
        "all-exhausted",
    )


def test_broken_candidate_does_not_kill_loop(live_auth):
    sw = _setup_two_accounts()
    # slot 1 looks healthy in the cache but its credential file is gone:
    # the engine's switch attempt must surface as an error event, not crash
    _seed_cache(UsageCache(), active_used=95, other_used=10)
    sw.store.find(1).credential_path().unlink()
    ev = AutoSwitchEngine(sw, opener=NoNetOpener()).tick()
    assert ev["event"] == "error" and "switch to slot 1 failed" in ev["message"]


def test_active_usage_unavailable_holds(live_auth):
    sw = _setup_two_accounts()
    # cache empty and opener broken -> active row status error -> hold
    ev = AutoSwitchEngine(sw, opener=NoNetOpener()).tick()
    assert ev["event"] == "no-switch" and ev["reason"] == "active-usage-unavailable"


def test_single_account_no_candidates(live_auth):
    sw = CodexAccountSwitcher()
    auth_path().write_text(
        make_auth_json(email="solo@x.io", account_id="acc-s", refresh_token="rt.1.s")
    )
    sw.add()
    UsageCache().put(1, _snapshot(99))
    ev = AutoSwitchEngine(sw, opener=NoNetOpener()).tick()
    assert ev["event"] == "no-switch" and ev["reason"] == "all-exhausted"


def test_dry_run_never_switches(live_auth):
    sw = _setup_two_accounts()
    _seed_cache(UsageCache(), active_used=95, other_used=10)
    engine = AutoSwitchEngine(sw, opener=NoNetOpener())
    ev = engine.run(once=True, dry_run=True)
    assert ev["event"] == "switch" and ev.get("dryRun") is True
    assert json.loads(auth_path().read_text())["tokens"]["account_id"] == "acc-b"  # untouched
    assert not (backup_root() / "autoswitch_state.json").exists()


def test_run_once_returns_and_run_loops(live_auth):
    sw = _setup_two_accounts()
    _seed_cache(UsageCache(), active_used=42, other_used=10)
    sleeps = []

    engine = AutoSwitchEngine(sw, opener=NoNetOpener(), sleep=sleeps.append)
    ev = engine.run(once=True)
    assert ev["reason"] == "below-threshold"
    assert sleeps == []  # once mode never sleeps

    engine2 = AutoSwitchEngine(
        sw,
        AutoConfig(interval_s=30),
        opener=NoNetOpener(),
        sleep=sleeps.append,
    )

    # stop the loop after two ticks via a raising sleep
    class Stop(Exception):
        pass

    def stop(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise Stop

    engine2.sleep = stop  # type: ignore[method-assign]
    with contextlib.suppress(Stop):
        engine2.run()
    assert sleeps == [30, 30]
