"""Usage API client for Codex's "wham" backend + token refresh.

Endpoint/protocol knowledge is borrowed from openusage (verified live there,
and re-verified here 2026-08-29 against codex-cli 0.150.1):

- ``GET https://chatgpt.com/backend-api/wham/usage`` returns the account's
  rate-limit windows. Minimal headers pass the edge: a plain ``User-Agent``
  is fine — no client impersonation needed. The response also mirrors the
  two decision numbers in ``x-codex-primary-used-percent`` /
  ``x-codex-secondary-used-percent`` response headers.
- ``POST https://auth.openai.com/oauth/token`` rotates tokens
  (``grant_type=refresh_token``, the Codex CLI's public client id).
  Refresh tokens ARE single-use: every successful refresh returns a new one,
  and refreshing a stale copy trips ``refresh_token_reused`` (openusage
  issue #516 — the codex CLI may have rotated the live ``auth.json``
  out-of-band). codex-swap therefore only ever refreshes *slot copies it
  exclusively owns*; the live file is left to the codex CLI (see
  switcher.usage_report).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USER_AGENT = "codex-swap"
DEFAULT_TIMEOUT_S = 10


class UsageAuthError(Exception):
    """The access token was rejected (401) — caller should refresh/relogin."""


class RefreshError(Exception):
    """A token refresh failed; ``kind`` classifies the server's verdict."""

    def __init__(self, kind: str, status: int):
        super().__init__(f"token refresh failed ({kind}, HTTP {status})")
        self.kind = kind  # expired | reused | invalidated | http | invalid


# --------------------------------------------------------------------- models


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    window_seconds: int
    reset_at: int  # epoch seconds

    @property
    def window_hours(self) -> float:
        return self.window_seconds / 3600


@dataclass(frozen=True)
class ModelLimit:
    name: str
    primary: UsageWindow | None
    secondary: UsageWindow | None


@dataclass(frozen=True)
class UsageSnapshot:
    email: str | None
    account_id: str | None
    plan_type: str | None
    primary: UsageWindow | None
    secondary: UsageWindow | None
    model_limits: tuple[ModelLimit, ...] = field(default_factory=tuple)
    limit_reached: bool = False
    fetched_at: float = field(default_factory=time.time)

    @property
    def binding_percent(self) -> float:
        """The utilization number a switcher would act on (highest window)."""
        pcts = [
            w.used_percent
            for w in (self.primary, self.secondary)
            if w is not None
        ]
        return max(pcts) if pcts else 0.0

    def to_json(self) -> dict:
        def win(w: UsageWindow | None) -> dict | None:
            if w is None:
                return None
            return {
                "usedPercent": w.used_percent,
                "windowSeconds": w.window_seconds,
                "resetsAt": w.reset_at,
            }

        return {
            "email": self.email,
            "accountId": self.account_id,
            "planType": self.plan_type,
            "primaryWindow": win(self.primary),
            "secondaryWindow": win(self.secondary),
            "modelLimits": [
                {
                    "name": m.name,
                    "primaryWindow": win(m.primary),
                    "secondaryWindow": win(m.secondary),
                }
                for m in self.model_limits
            ],
            "limitReached": self.limit_reached,
            "fetchedAt": self.fetched_at,
        }

    @staticmethod
    def from_json(data: dict) -> "UsageSnapshot":
        def win(w: dict | None) -> UsageWindow | None:
            if not w:
                return None
            return UsageWindow(
                used_percent=float(w.get("usedPercent", 0)),
                window_seconds=int(w.get("windowSeconds", 0)),
                reset_at=int(w.get("resetsAt", 0)),
            )

        return UsageSnapshot(
            email=data.get("email"),
            account_id=data.get("accountId"),
            plan_type=data.get("planType"),
            primary=win(data.get("primaryWindow")),
            secondary=win(data.get("secondaryWindow")),
            model_limits=tuple(
                ModelLimit(
                    name=m.get("name") or "?",
                    primary=win(m.get("primaryWindow")),
                    secondary=win(m.get("secondaryWindow")),
                )
                for m in data.get("modelLimits", [])
            ),
            limit_reached=bool(data.get("limitReached")),
            fetched_at=float(data.get("fetchedAt", 0)),
        )


# --------------------------------------------------------------------- parsing


def _parse_window(raw: object) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    reset = raw.get("reset_at")
    if not isinstance(used, (int, float)) or not isinstance(reset, (int, float)):
        return None
    return UsageWindow(
        used_percent=float(used),
        window_seconds=int(raw.get("limit_window_seconds") or 0),
        reset_at=int(reset),
    )


def parse_usage(payload: dict, fetched_at: float | None = None) -> UsageSnapshot:
    rate = payload.get("rate_limit")
    rate = rate if isinstance(rate, dict) else {}
    models: list[ModelLimit] = []
    extra = payload.get("additional_rate_limits")
    if isinstance(extra, list):
        for item in extra:
            if not isinstance(item, dict):
                continue
            inner = item.get("rate_limit")
            inner = inner if isinstance(inner, dict) else {}
            models.append(
                ModelLimit(
                    name=str(item.get("limit_name") or "?"),
                    primary=_parse_window(inner.get("primary_window")),
                    secondary=_parse_window(inner.get("secondary_window")),
                )
            )
    return UsageSnapshot(
        email=payload.get("email") if isinstance(payload.get("email"), str) else None,
        account_id=payload.get("account_id") if isinstance(payload.get("account_id"), str) else None,
        plan_type=payload.get("plan_type") if isinstance(payload.get("plan_type"), str) else None,
        primary=_parse_window(rate.get("primary_window")),
        secondary=_parse_window(rate.get("secondary_window")),
        model_limits=tuple(models),
        limit_reached=bool(rate.get("limit_reached")),
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


# -------------------------------------------------------------------- requests


def _open(opener, req, timeout: int):
    return (opener or urllib.request.urlopen)(req, timeout=timeout)


def fetch_usage(
    access_token: str,
    account_id: str | None,
    opener=None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> tuple[UsageSnapshot, dict]:
    """GET /wham/usage. Returns (snapshot, raw payload).

    Raises UsageAuthError on 401 so the caller can refresh-and-retry once.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(USAGE_URL, headers=headers)
    try:
        with _open(opener, req, timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise UsageAuthError("usage API rejected the access token") from e
        raise RefreshError(f"http-{e.code}", e.code) from e
    if not isinstance(payload, dict):
        raise RefreshError("invalid", 200)
    return parse_usage(payload), payload


def refresh_tokens(refresh_token: str, opener=None, timeout: int = 15) -> dict:
    """Rotate tokens at auth.openai.com; returns the new token fields.

    The response's ``refresh_token`` supersedes the one passed in — callers
    MUST persist it or the lineage is lost (single-use refresh tokens)."""
    from urllib.parse import quote

    body = (
        "grant_type=refresh_token"
        f"&client_id={quote(CLIENT_ID)}"
        f"&refresh_token={quote(refresh_token)}"
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with _open(opener, req, timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        kind = "http"
        if e.code in (400, 401):
            try:
                err = json.loads(e.read().decode()).get("error")
                if isinstance(err, dict):
                    kind = err.get("code") or err.get("error") or kind
                elif isinstance(err, str):
                    kind = err
            except Exception:
                pass
        raise RefreshError(kind, e.code) from e
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise RefreshError("invalid", 200)
    return payload
