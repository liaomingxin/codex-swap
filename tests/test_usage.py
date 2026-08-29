"""Usage client, cache, and the per-slot token policy."""

from __future__ import annotations

import io
import json
import time
import urllib.error

from conftest import make_access_token, make_auth_json

from codex_swap import usage as usage_api
from codex_swap.paths import auth_path
from codex_swap.switcher import CodexAccountSwitcher
from codex_swap.usage_store import UsageCache

# A response shaped like the real /wham/usage payload (captured 2026-08-29,
# trimmed to the fields the parser reads).
REAL_SHAPED = {
    "user_id": "user-X",
    "account_id": "acc-1234",
    "email": "user@example.com",
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 2,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 465990,
            "reset_at": int(time.time()) + 465990,
        },
        "secondary_window": None,
    },
    "additional_rate_limits": [
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 18000,
                    "reset_at": int(time.time()) + 18000,
                },
                "secondary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 604800,
                    "reset_at": int(time.time()) + 604800,
                },
            },
        }
    ],
}


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._buf = io.BytesIO(json.dumps(payload).encode())
        self.status = status

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Scripted opener: pops one response per call, recording requests."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)  # dict=200 body, int=HTTPError code
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, int):
            body = {"error": {"code": outcome}} if isinstance(outcome, str) else {}
            raise urllib.error.HTTPError(
                req.full_url, outcome, "faked", {}, io.BytesIO(json.dumps(body).encode())
            )
        return FakeResponse(outcome)


# ------------------------------------------------------------------ parse


def test_parse_usage_real_shape():
    snap = usage_api.parse_usage(REAL_SHAPED, fetched_at=123.0)
    assert snap.email == "user@example.com"
    assert snap.plan_type == "pro"
    assert snap.primary is not None and snap.primary.used_percent == 2
    assert snap.primary.window_seconds == 604800
    assert snap.secondary is None
    assert len(snap.model_limits) == 1
    spark = snap.model_limits[0]
    assert spark.name == "GPT-5.3-Codex-Spark"
    assert spark.primary.window_seconds == 18000
    assert spark.secondary.window_seconds == 604800
    assert snap.binding_percent == 2
    assert snap.fetched_at == 123.0


def test_snapshot_json_roundtrip():
    snap = usage_api.parse_usage(REAL_SHAPED)
    restored = usage_api.UsageSnapshot.from_json(snap.to_json())
    assert restored.to_json() == snap.to_json()


def test_snapshot_captures_supplementary_fields():
    payload = dict(
        REAL_SHAPED,
        credits={"balance": "12.50", "unlimited": False,
                 "approx_local_messages": [3, 17], "approx_cloud_messages": [0, 2]},
        rate_limit_reset_credits={"available_count": 2, "applicable_available_count": 1},
        code_review_rate_limit={"primary_window": {
            "used_percent": 9, "limit_window_seconds": 18000, "reset_at": 123,
        }},
        rate_limit_reached_type=None,
    )
    snap = usage_api.parse_usage(payload)
    assert snap.credits_balance == "12.50"
    assert snap.approx_local_messages == (3, 17)
    assert snap.approx_cloud_messages == (0, 2)
    assert snap.reset_credits_available == 2
    assert snap.reset_credits_applicable == 1
    assert snap.code_review is not None and snap.code_review.used_percent == 9
    # survives the cache roundtrip
    restored = usage_api.UsageSnapshot.from_json(snap.to_json())
    assert restored.credits_balance == "12.50"
    assert restored.reset_credits_available == 2
    assert restored.approx_local_messages == (3, 17)
    assert restored.code_review is not None


def test_parse_usage_degrades_to_empty():
    snap = usage_api.parse_usage({"rate_limit": None, "additional_rate_limits": "nope"})
    assert snap.primary is None and snap.model_limits == ()
    assert snap.binding_percent == 0.0


# ------------------------------------------------------------------ client


def test_fetch_usage_sends_minimal_headers():
    opener = FakeOpener(REAL_SHAPED)
    _, raw = usage_api.fetch_usage("tok", "acc-1234", opener=opener)
    assert raw["plan_type"] == "pro"
    req = opener.requests[0]
    assert req.get_header("User-agent") == "codex-swap"
    assert req.get_header("Chatgpt-account-id") == "acc-1234"
    assert "wham/usage" in req.full_url


def test_fetch_usage_401_raises_auth_error():
    opener = FakeOpener(401)
    try:
        usage_api.fetch_usage("tok", None, opener=opener)
        raise AssertionError("expected UsageAuthError")
    except usage_api.UsageAuthError:
        pass


def test_refresh_tokens_posts_rotated_fields():
    opener = FakeOpener(
        {"access_token": "new-acc", "refresh_token": "new-rt", "id_token": "new-id"}
    )
    payload = usage_api.refresh_tokens("old-rt", opener=opener)
    assert payload["refresh_token"] == "new-rt"
    req = opener.requests[0]
    body = req.data.decode()
    assert "grant_type=refresh_token" in body
    assert "app_EMoamEEZ73f0CkXaXp7hrann" in body


def test_refresh_tokens_classifies_reuse():
    # craft the exact server shape for refresh_token_reused
    class ReuseOpener(FakeOpener):
        def __call__(self, req, timeout=None):
            self.requests.append(req)
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "faked",
                {},
                io.BytesIO(json.dumps({"error": {"code": "refresh_token_reused"}}).encode()),
            )

    try:
        usage_api.refresh_tokens("old", opener=ReuseOpener())
        raise AssertionError("expected RefreshError")
    except usage_api.RefreshError as e:
        assert e.kind == "refresh_token_reused"


# ------------------------------------------------------------------- cache


def test_cache_ttl_and_stale_peek(tmp_path):
    cache = UsageCache()
    snap = usage_api.parse_usage(REAL_SHAPED, fetched_at=time.time() - 400)
    cache.put(1, snap)
    assert cache.get(1) is None  # older than TTL
    assert cache.peek(1) is not None  # but peekable for display


def test_cache_roundtrip_fresh(tmp_path):
    cache = UsageCache()
    snap = usage_api.parse_usage(REAL_SHAPED)
    cache.put(2, snap)
    got = cache.get(2)
    assert got is not None and got.primary.used_percent == 2


# ------------------------------------------------------- switcher policy


def _login_and_add_two(switcher: CodexAccountSwitcher):
    auth_path().write_text(
        make_auth_json(email="a@x.io", account_id="acc-a", refresh_token="rt.1.acc-a"),
        encoding="utf-8",
    )
    switcher.add()
    auth_path().write_text(
        make_auth_json(email="b@x.io", account_id="acc-b", refresh_token="rt.1.acc-b"),
        encoding="utf-8",
    )
    switcher.add()


def test_usage_report_active_uses_live_token_no_refresh(live_auth):
    sw = CodexAccountSwitcher()
    _login_and_add_two(sw)
    opener = FakeOpener(REAL_SHAPED, REAL_SHAPED)
    report = sw.usage_report(opener=opener)
    urls = [r.full_url for r in opener.requests]
    assert all("oauth/token" not in u for u in urls)  # live token never refreshed by us
    rows = {r["number"]: r for r in report["accounts"]}
    assert rows[2]["usageStatus"] == "ok" and rows[2]["active"] is True
    assert rows[2]["usage"]["planType"] == "pro"


def test_usage_report_inactive_expired_token_refreshes_once(live_auth):
    sw = CodexAccountSwitcher()
    _login_and_add_two(sw)
    # slot 1 (inactive) carries an expired access token
    entry1 = sw.store.find(1)
    expired_auth = json.loads(
        make_auth_json(
            email="a@x.io",
            account_id="acc-a",
            access_exp=time.time() - 60,
            refresh_token="rt.1.acc-a",
        )
    )
    sw.store.write_credential(entry1, json.dumps(expired_auth, indent=2))

    fresh_usage = dict(REAL_SHAPED, account_id="acc-a", email="a@x.io")
    refreshed_tokens = {
        "access_token": make_access_token("a@x.io", "acc-a", "pro", time.time() + 864000),
        "refresh_token": "rt.1.rotated-by-uswap",
    }
    # sequence per slot-1 fetch: usage(401) -> refresh(200) -> usage(200); slot 2: usage(200)
    opener = FakeOpener(401, refreshed_tokens, fresh_usage, REAL_SHAPED)
    report = sw.usage_report(force=True, opener=opener)

    row1 = next(r for r in report["accounts"] if r["number"] == 1)
    assert row1["usageStatus"] == "ok"
    assert any("oauth/token" in r.full_url for r in opener.requests)

    # rotated refresh token persisted + fingerprint updated so the slot stays findable
    stored = json.loads(sw.store.read_credential(sw.store.find(1)))
    assert stored["tokens"]["refresh_token"] == "rt.1.rotated-by-uswap"
    from codex_swap.identity import identity_from_auth

    ident = identity_from_auth(stored)
    assert sw.store.find_by_identity(ident).number == 1


def test_usage_report_dead_refresh_token_marks_auth_needed(live_auth):
    sw = CodexAccountSwitcher()
    _login_and_add_two(sw)
    entry1 = sw.store.find(1)
    expired_auth = json.loads(
        make_auth_json(
            email="a@x.io",
            account_id="acc-a",
            access_exp=time.time() - 60,
            refresh_token="rt.1.acc-a",
        )
    )
    sw.store.write_credential(entry1, json.dumps(expired_auth, indent=2))

    class DeadRefreshOpener(FakeOpener):
        """Usage 401s for acc-a (expired slot token), refresh is dead, others fine."""

        def __call__(self, req, timeout=None):
            self.requests.append(req)
            if "oauth/token" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url,
                    400,
                    "faked",
                    {},
                    io.BytesIO(
                        json.dumps({"error": {"code": "refresh_token_expired"}}).encode()
                    ),
                )
            if (
                "wham/usage" in req.full_url
                and req.get_header("Chatgpt-account-id") == "acc-a"
            ):
                raise urllib.error.HTTPError(req.full_url, 401, "faked", {}, io.BytesIO(b"{}"))
            return FakeResponse(dict(REAL_SHAPED, account_id="acc-a"))

    report = sw.usage_report(force=True, opener=DeadRefreshOpener())
    row1 = next(r for r in report["accounts"] if r["number"] == 1)
    assert row1["usageStatus"] == "auth-needed"
    assert row1.get("usage") is None


def test_usage_report_serves_cache_within_ttl(live_auth):
    sw = CodexAccountSwitcher()
    _login_and_add_two(sw)
    opener = FakeOpener(REAL_SHAPED, REAL_SHAPED)
    sw.usage_report(opener=opener)
    count_first = len(opener.requests)
    sw.usage_report(opener=opener)  # all fresh in cache: zero new requests
    assert len(opener.requests) == count_first


def test_usage_report_stale_on_error(live_auth):
    sw = CodexAccountSwitcher()
    _login_and_add_two(sw)
    good = FakeOpener(REAL_SHAPED, REAL_SHAPED)
    sw.usage_report(opener=good)  # seed the cache

    class DownOpener:
        def __call__(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "down", {}, io.BytesIO(b"{}"))

    report = sw.usage_report(force=True, opener=DownOpener())
    for row in report["accounts"]:
        assert row["usageStatus"] == "stale"  # last-good kept, marked
        assert row["usage"]["planType"] == "pro"  # data survives the outage
