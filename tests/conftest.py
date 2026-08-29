"""Shared fixtures: isolated CODEX_HOME + CODEX_SWAP_BACKUP per test."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from codex_swap.paths import auth_path, backup_root


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_access_token(email: str, account_id: str, plan: str, exp: float) -> str:
    return (
        _b64url({"alg": "RS256", "typ": "JWT"})
        + "."
        + _b64url(
            {
                "sub": f"goog|{email}",
                "exp": exp,
                "iat": exp - 864000,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                    "chatgpt_plan_type": plan,
                },
                "https://api.openai.com/profile": {"email": email},
            }
        )
        + ".sig"
    )


def make_id_token(email: str, account_id: str, plan: str, exp: float) -> str:
    return (
        _b64url({"alg": "RS256", "typ": "JWT"})
        + "."
        + _b64url(
            {
                "email": email,
                "email_verified": True,
                "name": "Test User",
                "exp": exp,
                "iat": exp - 3600,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                    "chatgpt_plan_type": plan,
                    "chatgpt_subscription_active_until": "2099-01-01",
                },
            }
        )
        + ".sig"
    )


def make_auth_json(
    email: str = "user@example.com",
    account_id: str = "acc-1234",
    plan: str = "pro",
    *,
    access_exp: float | None = None,
    refresh_token: str = "rt.1.test-refresh-token",
) -> str:
    """A realistic auth.json: exps default to far future (valid tokens)."""
    if access_exp is None:
        access_exp = time.time() + 10 * 86400
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": make_id_token(email, account_id, plan, time.time() + 3600),
                "access_token": make_access_token(email, account_id, plan, access_exp),
                "refresh_token": refresh_token,
                "account_id": account_id,
            },
            "last_refresh": "2026-08-29T00:00:00Z",
        },
        indent=2,
    )


@pytest.fixture(autouse=True)
def isolated_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own codex home and backup root via env vars."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SWAP_BACKUP", str(tmp_path / "codex-swap-backup"))
    (tmp_path / "codex-home").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def live_auth(isolated_homes: Path) -> Path:
    """An auth.json for user@example.com, logged in and valid."""
    path = auth_path()
    path.write_text(make_auth_json(), encoding="utf-8")
    return path


@pytest.fixture
def backup_root_path(isolated_homes: Path) -> Path:
    return backup_root()
