"""Account identity from Codex's OAuth tokens — parse, never verify.

``auth.json`` carries two JWTs we care about (signatures deliberately not
verified — we are a local switcher reading our own file, not an auth server):

- ``id_token``   — 1h lifetime. Top-level ``email``; the
  ``https://api.openai.com/auth`` namespace carries ``chatgpt_account_id``,
  ``chatgpt_plan_type`` and subscription dates. Identity only.
- ``access_token`` — ~10-day lifetime (measured 2026-08-29). This is what
  inference actually uses; its ``exp`` is the number a switcher watches.

Lineage identity for an account is the **refresh token hash**: refresh tokens
are (presumably) rotated on renewal while the account stays the same, and the
access token changes on every refresh, so either of those would make "same
account?" comparisons flap. Same idea as claude-swap's
``credential_fingerprint``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

AUTH_NS = "https://api.openai.com/auth"
PROFILE_NS = "https://api.openai.com/profile"


def _b64url_json(segment: str) -> dict | None:
    try:
        padded = segment + "=" * (-len(segment) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return data if isinstance(data, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def jwt_payload(token: str | None) -> dict | None:
    """Decode a JWT's payload segment; None for anything malformed."""
    if not token or token.count(".") != 2:
        return None
    return _b64url_json(token.split(".")[1])


def _claim(payload: dict | None, name: str) -> object:
    if payload is None:
        return None
    if name in payload:
        return payload[name]
    ns = payload.get(AUTH_NS) or payload.get(PROFILE_NS)
    if isinstance(ns, dict):
        return ns.get(name)
    return None


def _exp_datetime(payload: dict | None) -> datetime | None:
    exp = payload.get("exp") if payload else None
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=UTC)


@dataclass(frozen=True)
class AccountIdentity:
    """Everything codex-swap needs to know about a stored login."""

    email: str | None
    display_name: str | None
    account_id: str | None
    plan_type: str | None
    subscription_active_until: str | None
    access_expires_at: datetime | None
    id_expires_at: datetime | None
    refresh_fingerprint: str
    access_expired: bool

    @property
    def label(self) -> str:
        return self.email or self.account_id or "<unknown>"


def identity_from_auth(auth: dict) -> AccountIdentity | None:
    """Extract identity from a parsed auth.json dict; None if unusable.

    None means "this file cannot identify an account" — the caller refuses to
    manage (add/switch over) such a file rather than guessing.
    """
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return None

    id_payload = jwt_payload(tokens.get("id_token"))
    access_payload = jwt_payload(tokens.get("access_token"))

    email = _claim(id_payload, "email") or _claim(access_payload, "email")
    email = email if isinstance(email, str) and email else None

    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = _claim(id_payload, "chatgpt_account_id")
        account_id = account_id if isinstance(account_id, str) else None

    if email is None and account_id is None:
        return None

    auth_ns = id_payload.get(AUTH_NS) if isinstance(id_payload, dict) else None
    auth_ns = auth_ns if isinstance(auth_ns, dict) else {}

    plan = auth_ns.get("chatgpt_plan_type")
    display_name = _claim(id_payload, "name")
    access_exp = _exp_datetime(access_payload)
    refresh = tokens.get("refresh_token")
    fingerprint = (
        "sha256:" + hashlib.sha256(refresh.encode()).hexdigest()
        if isinstance(refresh, str) and refresh
        else "sha256-full:" + hashlib.sha256(json.dumps(auth).encode()).hexdigest()
    )

    return AccountIdentity(
        email=email,
        display_name=display_name if isinstance(display_name, str) else None,
        account_id=account_id,
        plan_type=plan if isinstance(plan, str) else None,
        subscription_active_until=(
            auth_ns.get("chatgpt_subscription_active_until")
            if isinstance(auth_ns.get("chatgpt_subscription_active_until"), str)
            else None
        ),
        access_expires_at=access_exp,
        id_expires_at=_exp_datetime(id_payload),
        refresh_fingerprint=fingerprint,
        access_expired=access_exp is not None and access_exp < datetime.now(UTC),
    )
