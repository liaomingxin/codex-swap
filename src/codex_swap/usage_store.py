"""Per-slot usage cache: last-good snapshots with a serve TTL.

Flat JSON under the backup root (``usage.json``)::

    {"schemaVersion": 1, "slots": {"1": {"snapshot": {...}, "fetchedAt": ...}}}

Same stale-on-error philosophy as claude-swap's usage store: a failed fetch
never blanks the last-good measurement, and rows carry their age so both the
human view and ``--json`` can say how fresh the number is. With ~1 request
per 5 minutes per slot we sit far inside any plausible edge budget.
"""

from __future__ import annotations

import time
from pathlib import Path

from codex_swap.atomic import atomic_write_json, read_json
from codex_swap.paths import backup_root
from codex_swap.usage import AccountStats, UsageSnapshot

SCHEMA_VERSION = 1
USAGE_FILENAME = "usage.json"
SERVE_TTL_S = 300  # a served snapshot is "current" for 5 minutes


def _cache_path() -> Path:
    return backup_root() / USAGE_FILENAME


class UsageCache:
    def _load_slots(self) -> dict:
        data = read_json(_cache_path())
        slots = data.get("slots") if data else None
        return slots if isinstance(slots, dict) else {}

    def get(self, slot: int, *, max_age_s: float | None = SERVE_TTL_S) -> UsageSnapshot | None:
        """The slot's cached snapshot, or None when absent/stale.

        ``max_age_s=None`` disables the staleness check."""
        row = self._load_slots().get(str(slot))
        if not isinstance(row, dict):
            return None
        fetched_at = row.get("fetchedAt")
        snapshot = row.get("snapshot")
        if not isinstance(fetched_at, (int, float)) or not isinstance(snapshot, dict):
            return None
        if max_age_s is not None and time.time() - fetched_at > max_age_s:
            return None
        try:
            return UsageSnapshot.from_json(snapshot)
        except (TypeError, ValueError):
            return None

    def get_stats(
        self, slot: int, *, max_age_s: float | None = SERVE_TTL_S
    ) -> AccountStats | None:
        row = self._load_slots().get(str(slot))
        if not isinstance(row, dict):
            return None
        stats = row.get("stats")
        fetched_at = row.get("fetchedAt")
        if not isinstance(stats, dict) or not isinstance(fetched_at, (int, float)):
            return None
        if max_age_s is not None and time.time() - fetched_at > max_age_s:
            return None
        try:
            return AccountStats.from_json(stats)
        except (TypeError, ValueError):
            return None

    def peek(self, slot: int) -> UsageSnapshot | None:
        """Last-good snapshot regardless of age (human display keeps it with an age note)."""
        return self.get(slot, max_age_s=None)

    def put(
        self, slot: int, snapshot: UsageSnapshot, stats: AccountStats | None = None
    ) -> None:
        slots = self._load_slots()
        row = slots.get(str(slot))
        row = row if isinstance(row, dict) else {}
        row["snapshot"] = snapshot.to_json()
        row["fetchedAt"] = snapshot.fetched_at
        if stats is not None:
            row["stats"] = stats.to_json()
        slots[str(slot)] = row
        atomic_write_json(_cache_path(), {"schemaVersion": SCHEMA_VERSION, "slots": slots})
