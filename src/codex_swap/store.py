"""The account store: slots, sequence file, per-slot credential files.

Layout under ``$CODEX_SWAP_BACKUP`` (default ``~/.codex-swap-backup``)::

    sequence.json                  # {"schemaVersion":1, "accounts":{"1":{...}}}
    credentials/1-<slug>.json      # verbatim auth.json content for slot 1
    unclaimed/<fp>-<ts>.json       # live logins stashed during a switch away

Slots are stable numbers (no renumbering on remove; new accounts take
``max+1``), matching claude-swap's UX so muscle memory transfers.

The slot credential file is a *byte-for-byte copy* of ``auth.json`` at save
time. Because Codex refreshes tokens roughly every 10 days and rewrites the
live file, the switcher refreshes the slot copy from the live file on every
switch *away* (see switcher.py) — otherwise restoring a stale snapshot would
discard a rotated refresh token and log the account out. Lesson learned the
hard way in claude-swap (#218/#237 lineage).
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from pathlib import Path

from codex_swap.atomic import atomic_write_json, atomic_write_text, read_json
from codex_swap.exceptions import StoreError
from codex_swap.identity import AccountIdentity
from codex_swap.paths import credentials_dir, sequence_path, unclaimed_dir

SCHEMA_VERSION = 1


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "account"


@dataclass
class SlotEntry:
    number: int
    email: str | None
    account_id: str | None
    plan_type: str | None
    added_at: float
    refresh_fingerprint: str
    slug: str

    def credential_path(self) -> Path:
        return credentials_dir() / f"{self.number}-{self.slug}.json"

    def to_json(self) -> dict:
        return {
            "email": self.email,
            "accountId": self.account_id,
            "planType": self.plan_type,
            "addedAt": self.added_at,
            "refreshFingerprint": self.refresh_fingerprint,
            "slug": self.slug,
        }


class AccountStore:
    """CRUD over slots + their credential files. No live-file knowledge."""

    def _load(self) -> dict:
        data = read_json(sequence_path())
        accounts = data.get("accounts") if data else None
        return accounts if isinstance(accounts, dict) else {}

    def _save(self, accounts: dict) -> None:
        atomic_write_json(
            sequence_path(),
            {"schemaVersion": SCHEMA_VERSION, "accounts": accounts},
        )

    def list_entries(self) -> list[SlotEntry]:
        entries: list[SlotEntry] = []
        for number, raw in self._load().items():
            if not isinstance(raw, dict):
                continue
            try:
                num = int(number)
            except ValueError:
                continue
            entries.append(
                SlotEntry(
                    number=num,
                    email=raw.get("email"),
                    account_id=raw.get("accountId"),
                    plan_type=raw.get("planType"),
                    added_at=float(raw.get("addedAt") or time.time()),
                    refresh_fingerprint=raw.get("refreshFingerprint") or "",
                    slug=raw.get("slug") or _slugify(raw.get("email") or f"slot{num}"),
                )
            )
        return sorted(entries, key=lambda e: e.number)

    def find(self, number: int) -> SlotEntry | None:
        return next((e for e in self.list_entries() if e.number == number), None)

    def find_by_identity(self, identity: AccountIdentity) -> SlotEntry | None:
        """Match by refresh fingerprint first (stable lineage), account_id second.

        Fingerprints diverge after a refresh-token rotation the slot never saw;
        account_id survives that, so it is the fallback, not the primary."""
        entries = self.list_entries()
        by_fp = {e.refresh_fingerprint for e in entries if e.refresh_fingerprint}
        if identity.refresh_fingerprint in by_fp:
            return next(
                e for e in entries if e.refresh_fingerprint == identity.refresh_fingerprint
            )
        if identity.account_id:
            return next((e for e in entries if e.account_id == identity.account_id), None)
        return None

    def upsert(self, identity: AccountIdentity) -> tuple[SlotEntry, bool]:
        """Add as a new slot, or refresh the matching slot's metadata.

        Returns (entry, created). Credential bytes are written separately by
        the caller (``write_credential``) so add and switch-away stash share
        one code path."""
        accounts = self._load()
        existing = self.find_by_identity(identity)
        if existing is not None:
            updated = SlotEntry(
                number=existing.number,
                email=identity.email or existing.email,
                account_id=identity.account_id or existing.account_id,
                plan_type=identity.plan_type or existing.plan_type,
                added_at=existing.added_at,
                refresh_fingerprint=identity.refresh_fingerprint,
                slug=existing.slug,
            )
            accounts[str(existing.number)] = updated.to_json()
            self._save(accounts)
            return updated, False

        number = max((int(k) for k in accounts), default=0) + 1
        slug = _slugify(identity.email or identity.account_id or f"slot{number}")
        entry = SlotEntry(
            number=number,
            email=identity.email,
            account_id=identity.account_id,
            plan_type=identity.plan_type,
            added_at=time.time(),
            refresh_fingerprint=identity.refresh_fingerprint,
            slug=slug,
        )
        accounts[str(number)] = entry.to_json()
        self._save(accounts)
        return entry, True

    def remove(self, number: int) -> SlotEntry:
        accounts = self._load()
        entry = self.find(number)
        if entry is None:
            raise StoreError(f"No account in slot {number}")
        del accounts[str(number)]
        self._save(accounts)
        path = entry.credential_path()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return entry

    def write_credential(self, entry: SlotEntry, credentials_text: str) -> None:
        atomic_write_text(entry.credential_path(), credentials_text)

    def read_credential(self, entry: SlotEntry) -> str:
        try:
            return entry.credential_path().read_text(encoding="utf-8")
        except OSError as e:
            raise StoreError(f"Credentials for slot {entry.number} are unreadable: {e}") from e

    def stash_unclaimed(self, credentials_text: str, fingerprint: str) -> Path:
        """Park an unrecognized live login instead of destroying it.

        A live auth.json that maps to no slot (fresh browser login the user
        never `add`ed) still contains the only copy of a refresh token —
        overwriting it blindly would revoke that login. Parked under
        unclaimed/, the user can inspect and re-add later."""
        unclaimed_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = unclaimed_dir() / f"{fingerprint[:16]}-{stamp}.json"
        atomic_write_text(path, credentials_text)
        return path
