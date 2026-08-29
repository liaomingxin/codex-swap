"""Auto-switch engine: poll usage, switch accounts before they hit limits.

Ported from claude-swap's UI-agnostic ``autoswitch.py`` (which documents its
policy in one paragraph — adapted here for Codex's window shapes):

When the active account's *binding window* (the highest utilization across
its account-level and per-model windows) crosses ``threshold_pct``, switch
to a candidate below the threshold, proactively — while the old account
still works, so a running codex picks the new one up on its next message.
Candidates must clear ``hysteresis_pct`` below the threshold so two accounts
hovering at the line never ping-flop, and a ``cooldown_s`` floor bounds the
switch rate (bypassed only when the active account is hard at its limit —
``limit_reached``). Freshness comes from the shared usage cache (5-minute
TTL), so a 60-second poll loop still costs ~1 request per 5 minutes per
slot. If usage can't be fetched, the engine stays put on last-known data —
it never switches on a guess.

State (``autoswitch_state.json`` under the backup root) persists the last
switch time so cron-driven ``cxswap auto --once`` ticks honor the cooldown
across processes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from codex_swap.atomic import atomic_write_json, read_json
from codex_swap.paths import backup_root
from codex_swap.switcher import CodexAccountSwitcher

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

EventFn = Callable[[dict], None]


@dataclass(frozen=True)
class AutoConfig:
    threshold_pct: float = 80.0
    hysteresis_pct: float = 5.0
    cooldown_s: float = 300.0
    interval_s: float = 60.0
    strategy: str = "best"  # "best" (most quota left) | "next" (rotate 1->2->3)


def _state_path():
    return backup_root() / STATE_FILENAME


def _binding_pct(row: dict) -> float | None:
    """Highest utilization across the row's account and per-model windows."""
    usage = row.get("usage")
    if not isinstance(usage, dict):
        return None

    def pct(window) -> float | None:
        if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)):
            return float(window["usedPercent"])
        return None

    values = [pct(usage.get("primaryWindow")), pct(usage.get("secondaryWindow"))]
    for model in usage.get("modelLimits", []):
        values.append(pct(model.get("primaryWindow")))
        values.append(pct(model.get("secondaryWindow")))
    values = [v for v in values if v is not None]
    return max(values) if values else None


class AutoSwitchEngine:
    """UI-agnostic: emits event dicts, never prints. CLI renders them."""

    def __init__(
        self,
        switcher: CodexAccountSwitcher,
        config: AutoConfig | None = None,
        *,
        on_event: EventFn | None = None,
        opener=None,
        sleep=time.sleep,
        now=time.time,
        dry_run: bool = False,
    ):
        self.switcher = switcher
        self.config = config or AutoConfig()
        self.on_event = on_event
        self.opener = opener
        self.sleep = sleep
        self.now = now
        self.dry_run = dry_run

    # ------------------------------------------------------------- state

    def _load_state(self) -> dict:
        data = read_json(_state_path())
        return data if isinstance(data, dict) else {}

    def _record_switch(self, from_slot: int | None, to_slot: int) -> None:
        atomic_write_json(
            _state_path(),
            {
                "schemaVersion": STATE_SCHEMA_VERSION,
                "lastSwitchAt": self.now(),
                "lastSwitchFrom": from_slot,
                "lastSwitchTo": to_slot,
            },
        )

    # -------------------------------------------------------------- emit

    def _emit(self, event: str, **fields) -> dict:
        payload = {"event": event, "ts": round(self.now(), 3), **fields}
        if self.on_event is not None:
            self.on_event(payload)
        return payload

    # -------------------------------------------------------------- tick

    def tick(self) -> dict:
        """One evaluation. Returns the event dict (also forwarded to on_event)."""
        cfg = self.config
        try:
            report = self.switcher.usage_report(opener=self.opener)
        except Exception as e:
            return self._emit("error", message=f"usage report failed: {e}")

        rows = report["accounts"]
        active_rows = [r for r in rows if r.get("active")]
        if not rows:
            return self._emit("no-switch", reason="no-accounts")
        if not active_rows:
            return self._emit(
                "no-switch",
                reason="unmanaged-active",
                hint="run `cxswap add` to manage the live login",
            )

        active = active_rows[0]
        if active.get("usageStatus") in ("auth-needed", "error") or not active.get("usage"):
            # Fail safe: no trustworthy numbers for the active account —
            # hold rather than switch on a guess.
            return self._emit(
                "no-switch", reason="active-usage-unavailable", slot=active["number"]
            )

        binding = _binding_pct(active)
        if binding is None:
            return self._emit(
                "no-switch", reason="active-usage-unavailable", slot=active["number"]
            )
        if binding < cfg.threshold_pct:
            return self._emit(
                "no-switch",
                reason="below-threshold",
                slot=active["number"],
                bindingPct=binding,
                thresholdPct=cfg.threshold_pct,
            )

        # Cooldown — bypassed only when the account is hard at its limit.
        state = self._load_state()
        last = state.get("lastSwitchAt")
        hard_limit = bool(active.get("usage", {}).get("limitReached"))
        if isinstance(last, (int, float)) and not hard_limit:
            remaining = cfg.cooldown_s - (self.now() - last)
            if remaining > 0:
                return self._emit(
                    "no-switch",
                    reason="cooldown",
                    slot=active["number"],
                    bindingPct=binding,
                    cooldownRemainingS=round(remaining),
                )

        # Candidates: managed, ok-or-stale usage, under the hysteresis line.
        candidates = []
        for row in rows:
            if row.get("active") or row.get("usageStatus") not in ("ok", "stale"):
                continue
            pct = _binding_pct(row)
            if pct is None:
                continue
            if pct < cfg.threshold_pct - cfg.hysteresis_pct:
                candidates.append((row, pct))

        if not candidates:
            above = sum(
                1
                for r in rows
                if (b := _binding_pct(r)) is not None and b >= cfg.threshold_pct
            )
            reason = "all-exhausted" if above >= len(rows) else "no-viable-candidate"
            return self._emit(
                "no-switch",
                reason=reason,
                slot=active["number"],
                bindingPct=binding,
                thresholdPct=cfg.threshold_pct,
            )

        if cfg.strategy == "next":
            numbers = sorted(r["number"] for r, _ in candidates)
            later = [n for n in numbers if n > active["number"]]
            target_num = later[0] if later else numbers[0]
            target_row = next(r for r, _ in candidates if r["number"] == target_num)
            target_pct = _binding_pct(target_row)
        else:  # best
            target_row, target_pct = min(candidates, key=lambda c: c[1])

        if self.dry_run:
            return self._emit(
                "switch",
                **{"from": active["number"], "fromBindingPct": binding},
                to=target_row["number"],
                toBindingPct=target_pct,
                email=target_row.get("email"),
                dryRun=True,
            )

        try:
            result = self.switcher.switch(str(target_row["number"]))
        except Exception as e:
            return self._emit(
                "error", message=f"switch to slot {target_row['number']} failed: {e}"
            )
        if not result.get("switched"):
            return self._emit(
                "no-switch",
                reason="switch-refused",
                slot=active["number"],
                detail=result,
            )
        self._record_switch(active["number"], target_row["number"])
        return self._emit(
            "switch",
            **{"from": active["number"], "fromBindingPct": binding},
            to=target_row["number"],
            toBindingPct=target_pct,
            email=target_row.get("email"),
        )

    # --------------------------------------------------------------- run

    def run(self, *, once: bool = False, dry_run: bool = False) -> dict:
        """Poll loop; ``once`` runs a single tick (cron mode)."""
        self.dry_run = dry_run
        while True:
            event = self.tick()
            if once:
                return event
            self.sleep(self.config.interval_s)
