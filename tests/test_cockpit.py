"""Cockpit import: decrypt, convert, freshness-aware upsert."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from conftest import make_auth_json

from codex_swap.cockpit import (
    ACCOUNTS_DIR,
    ACCOUNTS_INDEX,
    KEY_FILENAME,
    CockpitImportError,
    import_into_store,
    load_cockpit_accounts,
)
from codex_swap.store import AccountStore
from codex_swap.switcher import CodexAccountSwitcher

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@pytest.fixture
def cockpit_home(tmp_path):
    """A fake ~/.antigravity_cockpit: key file + encrypted accounts dir."""
    home = tmp_path / "cockpit"
    (home / ACCOUNTS_DIR).mkdir(parents=True)
    key = AESGCM.generate_key(bit_length=256)
    (home / KEY_FILENAME).write_bytes(base64.b64encode(key))
    return home, key


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _write_entry(
    home: Path, key: bytes, cockpit_id: str, email: str, account_id: str, iat: float
) -> None:
    record = {
        "id": cockpit_id,
        "email": email,
        "account_id": account_id,
        "plan_type": "pro",
        "tokens": {
            "id_token": "header.e30.sig",
            "access_token": (
                _b64url({"alg": "RS256", "typ": "JWT"})
                + "."
                + _b64url(
                    {
                        "exp": iat + 864000,
                        "iat": iat,
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": account_id,
                            "chatgpt_plan_type": "pro",
                        },
                        "https://api.openai.com/profile": {"email": email},
                    }
                )
                + ".sig"
            ),
            "refresh_token": f"rt.1.{account_id}",
        },
        "token_updated_at": iat,
    }
    nonce = b"0123456789ab"
    blob = AESGCM(key).encrypt(nonce, json.dumps(record).encode(), None)
    (home / ACCOUNTS_DIR / f"{cockpit_id}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codex",
                "algorithm": "AES-256-GCM",
                "key_id": "local-secure-account-storage-v1",
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(blob).decode(),
                "encrypted_at": int(time.time()),
            }
        )
    )


def _write_index(home: Path, entries: list[dict]) -> None:
    (home / ACCOUNTS_INDEX).write_text(
        json.dumps({"version": "1.0", "accounts": entries, "current_account_id": None})
    )


def test_import_adds_new_accounts(cockpit_home):
    home, key = cockpit_home
    _write_entry(home, key, "codex_a", "a@x.io", "acc-a", time.time() - 100)
    _write_index(home, [{"id": "codex_a", "email": "a@x.io", "plan_type": "pro"}])

    accounts = load_cockpit_accounts(str(home))
    assert len(accounts) == 1 and accounts[0].identity.email == "a@x.io"

    report = import_into_store(AccountStore(), accounts)
    assert report == [{"email": "a@x.io", "slot": 1, "action": "added"}]
    stored = json.loads(AccountStore().read_credential(AccountStore().find(1)))
    assert stored["tokens"]["account_id"] == "acc-a"
    assert stored["tokens"]["refresh_token"] == "rt.1.acc-a"
    assert stored["last_refresh"]  # folded from token_updated_at


def test_import_never_downgrades_to_older_tokens(live_auth, cockpit_home):
    """Slot 1 already holds today's live token; cockpit's copy is older."""
    sw = CodexAccountSwitcher()
    sw.add()  # slot 1: user@example.com / acc-1234, captured now

    home, key = cockpit_home
    _write_entry(
        home, key, "codex_same", "user@example.com", "acc-1234", time.time() - 5 * 86400
    )
    _write_index(home, [{"id": "codex_same", "email": "user@example.com"}])

    report = import_into_store(sw.store, load_cockpit_accounts(str(home)))
    assert report[0]["action"] == "kept-newer"
    assert report[0]["slot"] == 1
    # slot credential untouched: still the live capture's refresh token
    stored = json.loads(sw.store.read_credential(sw.store.find(1)))
    assert stored["tokens"]["refresh_token"] == "rt.1.test-refresh-token"


def test_import_refreshes_when_cockpit_is_newer(live_auth, cockpit_home):
    sw = CodexAccountSwitcher()
    sw.add()
    entry = sw.store.find(1)
    old_auth = json.loads(make_auth_json(access_exp=time.time() - 86400))
    sw.store.write_credential(entry, json.dumps(old_auth, indent=2))

    home, key = cockpit_home
    _write_entry(home, key, "codex_same", "user@example.com", "acc-1234", time.time())
    _write_index(home, [{"id": "codex_same", "email": "user@example.com"}])

    report = import_into_store(sw.store, load_cockpit_accounts(str(home)))
    assert report[0]["action"] == "refreshed"
    stored = json.loads(sw.store.read_credential(sw.store.find(1)))
    assert stored["tokens"]["refresh_token"] == "rt.1.acc-1234"


def test_wrong_key_is_a_clean_error(cockpit_home):
    home, key = cockpit_home
    _write_entry(home, key, "codex_a", "a@x.io", "acc-a", time.time())
    _write_index(home, [{"id": "codex_a", "email": "a@x.io"}])
    (home / KEY_FILENAME).write_bytes(base64.b64encode(AESGCM.generate_key(bit_length=256)))

    with pytest.raises(CockpitImportError, match="Decryption failed"):
        load_cockpit_accounts(str(home))


def test_missing_directory_is_a_clean_error():
    with pytest.raises(CockpitImportError, match="not found"):
        load_cockpit_accounts("/nonexistent/cockpit")
