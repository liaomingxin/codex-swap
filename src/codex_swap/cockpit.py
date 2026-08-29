"""Import accounts from Antigravity Cockpit Tools' encrypted store.

Cockpit (``~/.antigravity_cockpit``) keeps each Codex account as an
AES-256-GCM ciphertext next to a local key file::

    secure-account-storage.key        # base64(32-byte key), 0600
    codex_accounts.json               # index: active accounts (id, email, plan)
    codex_accounts/codex_<id>.json    # {"algorithm","key_id","nonce","ciphertext",...}

The GCM tag rides as the trailing 16 bytes of the ciphertext, no AAD —
verified against Cockpit Tools' writer on 2026-08-29. Decrypted, an entry
is Cockpit's own record: ``tokens`` (id/access/refresh) plus a top-level
``account_id`` we fold into the codex ``auth.json`` shape.

Freshness rule (the whole point of an import being non-destructive):
refresh tokens are single-use and rotate on every refresh, so *newer
access-token iat wins*. An incoming copy older than what a slot already
holds is skipped — importing must never resurrect an already-superseded
refresh token (which would trip ``refresh_token_reused`` on next use).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

from codex_swap.exceptions import CodexSwapError
from codex_swap.identity import AccountIdentity, identity_from_auth, jwt_payload

KEY_FILENAME = "secure-account-storage.key"
ACCOUNTS_INDEX = "codex_accounts.json"
ACCOUNTS_DIR = "codex_accounts"


class CockpitImportError(CodexSwapError):
    """Cockpit store unreadable, undecryptable, or malformed."""


@dataclass
class CockpitAccount:
    cockpit_id: str
    auth: dict  # codex auth.json shape
    identity: AccountIdentity
    token_updated_at: float  # cockpit's own clock for the token copy


def _cockpit_root(override: str | None) -> Path:
    root = Path(override).expanduser() if override else Path.home() / ".antigravity_cockpit"
    if not root.is_dir():
        raise CockpitImportError(f"Cockpit directory not found: {root}")
    return root


def _load_key(root: Path) -> bytes:
    key_file = root / KEY_FILENAME
    try:
        raw = base64.b64decode(key_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as e:
        raise CockpitImportError(f"Cannot read {key_file}: {e}") from e
    if len(raw) != 32:
        raise CockpitImportError(f"{key_file} is not a 32-byte key (got {len(raw)})")
    return raw


def _decrypt_entry(key: bytes, payload: dict) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        nonce = base64.b64decode(payload["nonce"])
        blob = base64.b64decode(payload["ciphertext"])
    except (KeyError, TypeError, ValueError) as e:
        raise CockpitImportError(f"Malformed encrypted entry: {e}") from e
    try:
        plain = AESGCM(key).decrypt(nonce, blob, None)
    except Exception as e:  # cryptography raises its own InvalidTag etc.
        raise CockpitImportError(
            f"Decryption failed (wrong key or corrupted entry): {e}"
        ) from e
    try:
        data = json.loads(plain)
    except json.JSONDecodeError as e:
        raise CockpitImportError(f"Decrypted entry is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise CockpitImportError("Decrypted entry is not an object")
    return data


def _to_auth_json(record: dict) -> dict:
    """Cockpit's record -> codex auth.json shape."""
    tokens = record.get("tokens")
    if not isinstance(tokens, dict):
        raise CockpitImportError("entry carries no tokens")
    out_tokens = {
        "id_token": tokens.get("id_token"),
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "account_id": record.get("account_id"),
    }
    updated = record.get("token_updated_at")
    last_refresh = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(updated)))
        if isinstance(updated, (int, float))
        else None
    )
    return {
        "OPENAI_API_KEY": None,
        "tokens": out_tokens,
        "last_refresh": last_refresh,
    }


def _access_iat(auth: dict) -> float:
    payload = jwt_payload(auth.get("tokens", {}).get("access_token"))
    iat = payload.get("iat") if payload else None
    return float(iat) if isinstance(iat, (int, float)) else 0.0


def load_cockpit_accounts(root_override: str | None = None) -> list[CockpitAccount]:
    """Decrypt every *active* account in Cockpit's index."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError as e:
        raise CockpitImportError(
            "import-cockpit needs the 'cryptography' package: "
            "uv tool install --editable '.[cockpit]' "
            "(or run via: uv run --with cryptography cxswap ...)"
        ) from e

    root = _cockpit_root(root_override)
    key = _load_key(root)
    index_path = root / ACCOUNTS_INDEX
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CockpitImportError(f"Cannot read {index_path}: {e}") from e

    out: list[CockpitAccount] = []
    for meta in index.get("accounts", []) if isinstance(index, dict) else []:
        if not isinstance(meta, dict):
            continue
        cockpit_id = meta.get("id")
        if not isinstance(cockpit_id, str):
            continue
        entry_path = root / ACCOUNTS_DIR / f"{cockpit_id}.json"
        try:
            payload = json.loads(entry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise CockpitImportError(f"Cannot read {entry_path}: {e}") from e
        record = _decrypt_entry(key, payload)
        auth = _to_auth_json(record)
        identity = identity_from_auth(auth)
        if identity is None:
            continue  # identity-free entries can't be managed; skip loudly-ish
        updated = record.get("token_updated_at")
        out.append(
            CockpitAccount(
                cockpit_id=cockpit_id,
                auth=auth,
                identity=identity,
                token_updated_at=float(updated) if isinstance(updated, (int, float)) else 0.0,
            )
        )
    return out


def import_into_store(store, accounts: list[CockpitAccount]) -> list[dict]:
    """Upsert decrypted accounts into the slot store, newest-token-wins.

    Returns one report row per account: added | refreshed | kept-newer."""
    report = []
    for account in accounts:
        existing = store.find_by_identity(account.identity)
        if existing is None:
            entry, _ = store.upsert(account.identity)
            store.write_credential(entry, json.dumps(account.auth, indent=2) + "\n")
            report.append(
                {"email": account.identity.email, "slot": entry.number, "action": "added"}
            )
            continue

        try:
            stored_auth = json.loads(store.read_credential(existing))
            stored_iat = _access_iat(stored_auth)
        except Exception:
            stored_iat = 0.0
        incoming_iat = _access_iat(account.auth)
        if incoming_iat > stored_iat:
            store.upsert(account.identity)
            store.write_credential(existing, json.dumps(account.auth, indent=2) + "\n")
            report.append(
                {
                    "email": account.identity.email,
                    "slot": existing.number,
                    "action": "refreshed",
                }
            )
        else:
            report.append(
                {
                    "email": account.identity.email,
                    "slot": existing.number,
                    "action": "kept-newer",
                }
            )
    return report
