"""Core orchestration: read live login, add/list/switch/remove accounts.

Verified experimentally on codex-cli 0.150.1 (2026-08-29, see README):

- every codex invocation re-reads ``auth.json`` (garbage file -> instant
  401, no cached-token fallback), so a swap takes effect on the *next*
  codex command — no restart dance, no Keychain cache;
- access tokens live ~10 days and refresh lazily, so write-collisions with
  a running codex are rare — but the switch-away stash below still keeps
  us correct when they happen.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

from codex_swap.atomic import atomic_write_text
from codex_swap.identity import AccountIdentity, identity_from_auth
from codex_swap.paths import auth_path, backup_root, unclaimed_dir
from codex_swap.store import AccountStore, SlotEntry
from codex_swap.exceptions import AccountNotFoundError, AuthFileError, SwitchError


class CodexAccountSwitcher:
    def __init__(self) -> None:
        self.store = AccountStore()

    # ------------------------------------------------------------------ live

    def read_live(self) -> tuple[str, AccountIdentity]:
        """The live auth.json bytes + parsed identity.

        Raises AuthFileError when the file is absent or not JSON — the caller
        decides whether that's fatal (``add``) or just nothing-to-stash
        (``switch``)."""
        try:
            text = auth_path().read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise AuthFileError(
                "No auth.json in CODEX_HOME — run `codex login` first"
            ) from e
        except OSError as e:
            raise AuthFileError(f"Cannot read {auth_path()}: {e}") from e
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError as e:
            raise AuthFileError(
                f"{auth_path()} is not valid JSON ({e}); refusing to manage it"
            ) from e
        identity = identity_from_auth(data)
        if identity is None:
            raise AuthFileError(
                "auth.json carries no recognizable account identity "
                "(no email/account_id in its tokens)"
            )
        return text, identity

    def active_slot(self) -> SlotEntry | None:
        try:
            _, identity = self.read_live()
        except AuthFileError:
            return None
        return self.store.find_by_identity(identity)

    # ------------------------------------------------------------------ add

    def add(self) -> SlotEntry:
        """Capture the current login as a managed account slot."""
        text, identity = self.read_live()
        entry, created = self.store.upsert(identity)
        self.store.write_credential(entry, text)
        if not created:
            # refresh-in-place: tokens may have rotated since the slot was
            # first captured; the live file is always the truth
            pass
        return entry

    # ----------------------------------------------------------- list/status

    def list_payload(self) -> dict:
        entries = self.store.list_entries()
        active = self.active_slot()
        accounts = []
        for entry in entries:
            try:
                cred_text = self.store.read_credential(entry)
                cred_identity = identity_from_auth(json.loads(cred_text))
            except Exception:
                cred_identity = None
            row = {
                "number": entry.number,
                "email": entry.email,
                "plan": entry.plan_type,
                "active": active is not None and entry.number == active.number,
            }
            if cred_identity is not None and cred_identity.access_expires_at:
                row["accessTokenExpiresAt"] = cred_identity.access_expires_at.isoformat()
                row["accessExpired"] = cred_identity.access_expired
            accounts.append(row)
        return {
            "schemaVersion": 1,
            "activeAccountNumber": active.number if active else None,
            "accounts": accounts,
        }

    # ---------------------------------------------------------------- switch

    def _resolve_target(self, target: str | None) -> SlotEntry:
        entries = self.store.list_entries()
        if not entries:
            raise SwitchError("No accounts stored — run `cxswap add` first")
        if target is not None:
            if target.isdigit():
                hit = next((e for e in entries if e.number == int(target)), None)
                if hit is not None:
                    return hit
            needle = target.lower()
            hit = next(
                (e for e in entries if (e.email or "").lower() == needle), None
            )
            if hit is not None:
                return hit
            raise AccountNotFoundError(f"No account matches '{target}'")

        active = self.active_slot()
        if active is None:
            return entries[0]
        later = [e for e in entries if e.number > active.number]
        return later[0] if later else entries[0]

    def switch(self, target: str | None = None) -> dict:
        """Swap the live login, stashing the outgoing one first.

        Ordering matters: the outgoing credential must be saved (or parked)
        *before* the target lands, because os.replace is the point of no
        return for the only refresh-token copy."""
        entry = self._resolve_target(target)

        stash_note = None
        try:
            live_text, live_identity = self.read_live()
        except AuthFileError as e:
            live_text, live_identity, stash_note = None, None, str(e)

        if live_identity is not None:
            live_slot = self.store.find_by_identity(live_identity)
            if live_slot is not None and live_slot.number == entry.number:
                return {
                    "switched": False,
                    "to": self._entry_ref(entry),
                    "reason": "already active",
                    "stashed": None,
                }
            if live_slot is not None:
                # refresh-in-place for the outgoing slot: keep any newer
                # tokens the live file accumulated (10-day refresh cycle)
                self.store.write_credential(live_slot, live_text)
                self.store.upsert(live_identity)
            else:
                path = self.store.stash_unclaimed(live_text, live_identity.refresh_fingerprint)
                stash_note = f"unrecognized login parked at {path} (`cxswap add` it, or drop it)"

        credentials_text = self.store.read_credential(entry)
        try:
            atomic_write_text(auth_path(), credentials_text)
        except OSError as e:
            raise SwitchError(f"Failed to write {auth_path()}: {e}") from e

        return {
            "switched": True,
            "to": self._entry_ref(entry),
            "stashed": stash_note,
        }

    @staticmethod
    def _entry_ref(entry: SlotEntry) -> dict:
        return {
            "number": entry.number,
            "email": entry.email,
            "plan": entry.plan_type,
        }

    # --------------------------------------------------------------- remove

    def remove(self, number: int) -> SlotEntry:
        active = self.active_slot()
        entry = self.store.remove(number)
        if active is not None and active.number == number:
            # the removed account is still the live login; leave the live
            # file alone (removing data ≠ logging out), just note it
            pass
        return entry

    # ----------------------------------------------------------------- purge

    def purge(self) -> None:
        root = backup_root()
        if root.exists():
            shutil.rmtree(root)

    # ------------------------------------------------------------- unclaimed

    def list_unclaimed(self) -> list[dict]:
        out = []
        directory = unclaimed_dir()
        if not directory.exists():
            return out
        for path in sorted(directory.glob("*.json")):
            try:
                identity = identity_from_auth(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                identity = None
            out.append(
                {
                    "file": path.name,
                    "email": identity.email if identity else None,
                    "fingerprint": identity.refresh_fingerprint[:16] if identity else None,
                    "mtime": datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                }
            )
        return out
