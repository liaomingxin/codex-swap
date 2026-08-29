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
from datetime import datetime, timezone

from codex_swap import __version__
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
    remaining = dt - datetime.now(timezone.utc)
    days = remaining.days if remaining.total_seconds() >= 0 else 0
    marker = " EXPIRED" if expired else ""
    return f"{days}d{marker}" if days else "expired"


def _cmd_add(args: argparse.Namespace) -> int:
    switcher = CodexAccountSwitcher()
    entry = switcher.add()
    _say(args, f"Stored account {entry.number} ({entry.email or entry.account_id}"
         f"{f', {entry.plan_type}' if entry.plan_type else ''})")
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
            (r for r in payload["accounts"] if (r["email"] or "").lower() == args.target.lower()),
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


def _fmt_reset(reset_at: int | None) -> str:
    if not reset_at:
        return "-"
    remaining = reset_at - time.time()
    if remaining <= 0:
        return "resetting"
    hours = remaining / 3600
    if hours >= 48:
        return f"{hours / 24:.0f}d"
    if hours >= 1:
        return f"{hours:.0f}h"
    return f"{remaining / 60:.0f}m"


def _fmt_window(window: dict | None) -> str:
    if not window:
        return "-"
    hours = window.get("windowSeconds", 0) / 3600
    tag = f"{hours:.0f}h" if hours < 168 else f"{hours / 24:.0f}d"
    return f"{window.get('usedPercent', 0):.0f}% ({tag})"


def _cmd_usage(args: argparse.Namespace) -> int:
    payload = CodexAccountSwitcher().usage_report(force=args.refresh)
    if args.json:
        print(json.dumps(payload))
        return 0
    if not payload["accounts"]:
        _out("No accounts stored. Run `codex login`, then `cxswap add`.")
        return 0
    _say(args, f"{'*':<2} {'#':<4} {'EMAIL':<30} {'PLAN':<6} {'WINDOW':<14} {'5H':<12} {'RESETS IN'}")
    for row in payload["accounts"]:
        mark = "*" if row["active"] else " "
        usage = row.get("usage")
        status = row.get("usageStatus", "ok")
        if usage:
            plan = usage.get("planType") or row.get("plan") or "-"
            window = _fmt_window(usage.get("primaryWindow"))
            # account-level primary is weekly-shaped; any 5h-shaped window
            # (account secondary or a model's primary) is the burst budget
            five_hour = "-"
            candidates = [usage.get("secondaryWindow")] + [
                m.get("primaryWindow") for m in usage.get("modelLimits", [])
            ]
            for cand in candidates:
                if cand and (cand.get("windowSeconds") or 0) <= 6 * 3600:
                    five_hour = _fmt_window(cand)
                    break
            reset = usage.get("primaryWindow", {}).get("resetsAt")
            age_note = ""
            age = row.get("usageAgeSeconds")
            if status == "stale" and isinstance(age, (int, float)):
                age_note = f"  · {age // 60}m ago"
            _say(args, f"{mark:<2} {row['number']:<4} {(row.get('email') or '?')[:29]:<30} {plan:<6} {window:<14} {five_hour:<12} {_fmt_reset(reset)}{age_note}")
            for m in usage.get("modelLimits", []):
                name = (m.get("name") or "?")[:24]
                _say(args, f"{'':>8}{name:<26} {_fmt_window(m.get('primaryWindow')):<14} {_fmt_window(m.get('secondaryWindow'))}")
        else:
            plan = row.get("plan") or "-"
            note = {
                "auth-needed": "token expired — switch to it and run codex once, then re-add",
                "stale": "no data yet",
                "error": row.get("usageError", "unavailable"),
            }.get(status, "no data")
            _say(args, f"{mark:<2} {row['number']:<4} {(row.get('email') or '?')[:29]:<30} {plan:<6} {note}")
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
