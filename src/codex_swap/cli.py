"""Command line front end for codex-swap.

Subcommand structure (no pre-dispatch hacks): ``cxswap [--json] <cmd>``.
Human-readable output goes to stdout, ``--json`` payloads are single objects
on stdout with notices on stderr — same contract claude-swap settled on,
so scripts port over unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

from codex_swap import __version__, display
from codex_swap.exceptions import CodexSwapError
from codex_swap.switcher import CodexAccountSwitcher


def _out(text: str) -> None:
    """Notices and errors — always stderr, never polluting JSON on stdout."""
    print(text, file=sys.stderr)


def _say(args: argparse.Namespace, text: str) -> None:
    """Human-readable output: stdout normally, stderr when --json owns stdout."""
    print(text, file=sys.stderr if getattr(args, "json", False) else sys.stdout)


def _fmt_exp(iso: str | None, expired: bool | None) -> str:
    if iso is None:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    remaining = dt - datetime.now(UTC)
    days = remaining.days if remaining.total_seconds() >= 0 else 0
    marker = " EXPIRED" if expired else ""
    return f"{days}d{marker}" if days else "expired"


def _cmd_add(args: argparse.Namespace) -> int:
    switcher = CodexAccountSwitcher()
    entry = switcher.add()
    _say(
        args,
        f"Stored account {entry.number} ({entry.email or entry.account_id}"
        f"{f', {entry.plan_type}' if entry.plan_type else ''})",
    )
    if args.json:
        print(json.dumps({"added": True, **switcher._entry_ref(entry)}))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    payload = CodexAccountSwitcher().list_payload()
    if args.json:
        print(json.dumps(payload))
        return 0
    if not payload["accounts"]:
        _out("No accounts stored. Run `codex login`, then `cxswap add`.")
        return 0
    active_num = payload["activeAccountNumber"]
    _say(args, f"{'*':<2} {'#':<4} {'EMAIL':<34} {'PLAN':<8} {'ACCESS TOKEN'}")
    for row in payload["accounts"]:
        mark = "*" if row["active"] else " "
        plan = row["plan"] or "-"
        exp = _fmt_exp(row.get("accessTokenExpiresAt"), row.get("accessExpired"))
        _say(args, f"{mark:<2} {row['number']:<4} {row['email'] or '?':<34} {plan:<8} {exp}")
    if active_num is None:
        _out("(*) active account is not managed — run `cxswap add` to capture it")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    switcher = CodexAccountSwitcher()
    try:
        _, identity = switcher.read_live()
    except CodexSwapError as e:
        _out(f"Error: {e}")
        return 1
    slot = switcher.active_slot()
    who = identity.email or identity.account_id or "?"
    where = f" (slot {slot.number})" if slot else " (unmanaged — run `cxswap add`)"
    exp = _fmt_exp(
        identity.access_expires_at.isoformat() if identity.access_expires_at else None,
        identity.access_expired,
    )
    _say(args, f"Active: {who}{where} — access token: {exp}")
    if args.json:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "email": identity.email,
                    "accountId": identity.account_id,
                    "plan": identity.plan_type,
                    "slot": slot.number if slot else None,
                    "accessTokenExpiresAt": (
                        identity.access_expires_at.isoformat()
                        if identity.access_expires_at
                        else None
                    ),
                    "accessExpired": identity.access_expired,
                }
            )
        )
    return 0


def _cmd_switch(args: argparse.Namespace) -> int:
    result = CodexAccountSwitcher().switch(args.target)
    to = result["to"]
    if result["switched"]:
        _say(args, f"Switched to account {to['number']} ({to['email']})")
    else:
        _say(args, f"Account {to['number']} ({to['email']}) is already active")
    if result.get("stashed"):
        _out(f"Note: {result['stashed']}")
    if args.json:
        print(json.dumps({"schemaVersion": 1, **result}))
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    switcher = CodexAccountSwitcher()
    if args.target.isdigit():
        number = int(args.target)
    else:
        payload = switcher.list_payload()
        hit = next(
            (
                r
                for r in payload["accounts"]
                if (r["email"] or "").lower() == args.target.lower()
            ),
            None,
        )
        if hit is None:
            _out(f"Error: no account matches '{args.target}'")
            return 1
        number = hit["number"]
    if not args.yes:
        answer = input(f"Remove account {number} from codex-swap? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            _out("Aborted")
            return 1
    entry = switcher.remove(number)
    _out(f"Removed account {entry.number} ({entry.email})")
    return 0


def _cmd_unclaimed(args: argparse.Namespace) -> int:
    rows = CodexAccountSwitcher().list_unclaimed()
    if args.json:
        print(json.dumps({"schemaVersion": 1, "unclaimed": rows}))
        return 0
    if not rows:
        _out("No unclaimed credentials")
        return 0
    for row in rows:
        _out(f"{row['file']}  {row['email'] or '?'}")
    return 0


def _window_row(label: str, window: dict, now: float) -> str:
    """One account-level window line: label + bar + pct + reset times."""
    pct = float(window.get("usedPercent", 0))
    bar_txt = display.bar(pct)
    pct_txt = display.paint(f"{pct:3.0f}%", display.health_color(pct))
    hit = (
        "  " + display.paint("LIMIT REACHED", "\x1b[31m") if window.get("limitReached") else ""
    )
    reset = display.fmt_reset(window.get("resetsAt"), now=now)
    return f"  {label:<8} {bar_txt}  {pct_txt}   {reset}{hit}"


def _model_lines(model_limits: list[dict], now: float) -> list[str]:
    """Per-model limit rows, compact and dim (they ride below the mains)."""
    lines = []
    for m in model_limits:
        parts = []
        for key in ("primaryWindow", "secondaryWindow"):
            w = m.get(key)
            if not w:
                continue
            pct = float(w.get("usedPercent", 0))
            label = display.window_label(w.get("windowSeconds", 0))
            reset = display.fmt_reset(w.get("resetsAt"), now=now)
            pct_txt = display.paint(f"{pct:.0f}%", display.health_color(pct))
            parts.append(f"{label} {pct_txt} → {reset}")
        if parts:
            name = (m.get("name") or "?")[:28]
            lines.append(f"  {display.paint('·', '\x1b[36m')} {name:<30} {'   '.join(parts)}")
    return lines


def _account_card(row: dict, now: float) -> list[str]:
    """A card per account: header + window bars + model limits."""
    usage = row.get("usage")
    number = row["number"]
    email = (row.get("email") or "?")[:36]
    plan = (usage or {}).get("planType") or row.get("plan") or "-"

    if row.get("active"):
        marker = display.paint("●", "\x1b[36m\x1b[1m")
        tail = display.paint("◀ active", "\x1b[36m")
        header = f"{marker} {display.bold(f'{number}  {email}')}"
        plan_txt = display.dim(f"· {plan}")
        head_line = f"{header} {plan_txt}  {tail}"
    else:
        marker = display.paint("○", "\x1b[2m")
        head_line = f"{marker} {number}  {email} {display.dim(f'· {plan}')}"

    # token freshness rides on the header's meta when we know it
    lines = [head_line]
    if not usage:
        status = row.get("usageStatus", "ok")
        note = {
            "auth-needed": "token expired — switch here and run codex once, then re-add",
            "error": row.get("usageError", "unavailable"),
        }.get(status, "no data yet")
        lines.append(f"  {display.paint(note, '\x1b[33m')}")
        return lines

    primary = usage.get("primaryWindow")
    secondary = usage.get("secondaryWindow")
    if primary:
        label = display.window_label(primary.get("windowSeconds", 0))
        lines.append(_window_row(label, primary, now))
    if secondary:
        label = display.window_label(secondary.get("windowSeconds", 0))
        lines.append(_window_row(label, secondary, now))
    lines.extend(_model_lines(usage.get("modelLimits", []), now))

    age = row.get("usageAgeSeconds")
    status = row.get("usageStatus", "ok")
    meta = []
    if usage.get("limitReached"):
        meta.append(display.paint("limit reached", "\x1b[31m"))
    if status == "stale" and isinstance(age, (int, float)):
        meta.append(display.dim(f"cached {display.fmt_age(age)}"))
    if meta:
        lines.append(f"  {'   '.join(meta)}")
    return lines


def _cmd_usage(args: argparse.Namespace) -> int:
    payload = CodexAccountSwitcher().usage_report(force=args.refresh)
    if args.json:
        print(json.dumps(payload))
        return 0
    if not payload["accounts"]:
        _out("No accounts stored. Run `codex login`, then `cxswap add`.")
        return 0
    now = time.time()
    blocks = [_account_card(row, now) for row in payload["accounts"]]
    sep = display.dim("─" * 68)
    for i, block in enumerate(blocks):
        if i:
            print(sep)
        for line in block:
            print(line)
    return 0


def _fmt_event_human(event: dict) -> str:
    kind = event.get("event")
    slot = event.get("slot")
    if kind == "switch":
        return (
            f"switched {event.get('from')} -> {event.get('to')} "
            f"({event.get('email') or '?'}; {event.get('fromBindingPct', 0):.0f}% -> "
            f"{event.get('toBindingPct', 0):.0f}%"
            + (" [dry-run]" if event.get("dryRun") else "")
            + ")"
        )
    if kind == "no-switch":
        reason = event.get("reason")
        detail = {
            "below-threshold": (
                f"active {slot} at {event.get('bindingPct', 0):.0f}% "
                f"< {event.get('thresholdPct', 0):.0f}%"
            ),
            "cooldown": f"cooldown {event.get('cooldownRemainingS', 0):.0f}s remaining",
            "all-exhausted": "every account is at or over the threshold",
            "no-viable-candidate": "no candidate clears the hysteresis margin",
            "active-usage-unavailable": f"active {slot} usage unavailable — holding",
            "unmanaged-active": (event.get("hint") or "unmanaged active login"),
            "no-accounts": "no accounts stored",
            "switch-refused": "switch refused",
        }.get(reason, reason or "")
        return f"staying ({detail})"
    if kind == "error":
        return f"error: {event.get('message')}"
    return str(event)


def _cmd_auto(args: argparse.Namespace) -> int:
    from codex_swap.engine import AutoConfig, AutoSwitchEngine

    config = AutoConfig(
        threshold_pct=args.threshold,
        interval_s=args.interval,
        strategy=args.strategy,
    )

    def emit(event: dict) -> None:
        if args.json:
            print(json.dumps({"schemaVersion": 1, **event}), flush=True)
        elif not args.quiet:
            stamp = time.strftime("%H:%M:%S")
            _say(args, f"{stamp}  {_fmt_event_human(event)}")

    engine = AutoSwitchEngine(CodexAccountSwitcher(), config, on_event=emit)
    try:
        event = engine.run(once=args.once, dry_run=args.dry_run)
    except CodexSwapError as e:
        _out(f"Error: {e}")
        return 1
    if args.once:
        # claude-swap parity: 0 switched, 1 error, 2 nothing to do, 3 blocked
        if event["event"] == "error":
            return 1
        if event["event"] == "switch":
            return 0
        reason = event.get("reason")
        return 3 if reason in ("cooldown", "all-exhausted", "no-viable-candidate") else 2
    return 0


def _cmd_import_cockpit(args: argparse.Namespace) -> int:
    from codex_swap.cockpit import import_into_store, load_cockpit_accounts
    from codex_swap.store import AccountStore

    accounts = load_cockpit_accounts(args.path)
    if not accounts:
        _say(args, "No active Codex accounts found in Cockpit")
        if args.json:
            print(json.dumps({"schemaVersion": 1, "imported": [], "count": 0}))
        return 0
    report = import_into_store(AccountStore(), accounts)
    for row in report:
        note = {
            "added": "stored as new slot",
            "refreshed": "updated slot with newer tokens",
            "kept-newer": "skipped — slot already holds newer tokens",
        }[row["action"]]
        _say(args, f"slot {row['slot']}  {row['email']}  ({note})")
    if args.json:
        print(json.dumps({"schemaVersion": 1, "imported": report, "count": len(report)}))
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input("Remove ALL codex-swap data (stored accounts)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            _out("Aborted")
            return 1
    CodexAccountSwitcher().purge()
    _out("All codex-swap data removed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cxswap",
        description="Multi-account switcher for the OpenAI Codex CLI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON on stdout (notices go to stderr)",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("add", help="Store the current codex login as an account")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("list", help="List stored accounts")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("status", help="Show the active account")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("switch", help="Rotate to the next account, or a specific one")
    p.add_argument("target", nargs="?", metavar="NUM|EMAIL", help="Account to switch to")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_switch)

    p = sub.add_parser("remove", help="Remove a stored account")
    p.add_argument("target", metavar="NUM|EMAIL")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=_cmd_remove)

    p = sub.add_parser("unclaimed", help="List stashed unclaimed logins")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_unclaimed)

    p = sub.add_parser("usage", help="Per-account rate-limit usage (weekly + 5h windows)")
    p.add_argument("--refresh", action="store_true", help="Bypass the 5-minute cache")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_usage)

    p = sub.add_parser(
        "auto",
        help="Watch usage and switch before the active account hits its limit",
        description=(
            "Polls the usage cache and, when the active account's highest window "
            "crosses the threshold, switches to a healthier account. Run bare for "
            "a foreground loop, or --once from cron."
        ),
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        metavar="PCT",
        help="Switch when the binding window reaches this percent (default 80)",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=60.0,
        metavar="SEC",
        help="Poll interval for the foreground loop (default 60s)",
    )
    p.add_argument(
        "--strategy",
        choices=("best", "next"),
        default="best",
        help="best: most quota left (default); next: rotate 1->2->3",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Single check-and-switch, for cron/scripts (see exit codes)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Log what would happen, never switch"
    )
    p.add_argument("--quiet", action="store_true", help="Human mode: suppress per-tick lines")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_auto)

    p = sub.add_parser(
        "import-cockpit",
        help="Import Codex accounts from Cockpit Tools (encrypted store or export file)",
        description=(
            "Imports Codex accounts as cxswap slots, newest-token-wins. "
            "Default source: ~/.antigravity_cockpit's encrypted store "
            "(AES-256-GCM, local key file). With --file: a Cockpit UI export "
            "(plaintext JSON, no key needed, may come from another machine)."
        ),
    )
    p.add_argument(
        "--path", metavar="DIR", help="Cockpit directory (default ~/.antigravity_cockpit)"
    )
    p.add_argument(
        "--file",
        metavar="JSON",
        help="Import a Cockpit UI export file instead of the local store",
    )
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_import_cockpit)

    p = sub.add_parser("purge", help="Remove all codex-swap data")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=_cmd_purge)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    # Per-subcommand --json wins over the global flag (both accepted).
    try:
        code = args.func(args)
    except CodexSwapError as e:
        _out(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        _out("\nCancelled")
        sys.exit(130)
    sys.exit(code)


if __name__ == "__main__":
    main()
