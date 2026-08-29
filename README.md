# codex-swap

Multi-account switcher for the [OpenAI Codex CLI](https://github.com/openai/codex).
Store several ChatGPT logins, switch between them with one command — no
browser re-login, no token juggling.

## Installation

From a checkout (until the first PyPI release):

```bash
git clone https://github.com/liaomingxin/codex-swap.git
cd codex-swap
uv tool install --editable .    # `cxswap` on PATH, live-reloads your edits
```

Or run straight from the checkout without installing:

```bash
uv sync
uv run cxswap --help
```

Requires Python 3.12+ and the Codex CLI installed and logged in once.

```
codex login            # log in with account #1 (browser)
cxswap add             # capture it
codex login            # log in with account #2
cxswap add
cxswap list            # dashboard: slots, plans, token expiry
cxswap switch          # rotate; or `cxswap switch 2` / `cxswap switch me@x.io`
```

## Why this works (verified, not assumed)

Codex keeps its login in **one plain file on every platform** —
`$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) — with no Keychain,
no registry, no daemon. Two experiments (2026-08-29, codex-cli 0.150.1)
grounded the design:

1. **Every codex invocation re-reads `auth.json`.** Replacing the file with
   garbage made the very next `codex exec` fail with `401 Unauthorized` —
   no cached-token fallback anywhere. So a swap takes effect on the next
   codex command: no restarts, no cache-expiry waiting.
2. **Access tokens live ~10 days** and refresh lazily (the `id_token`'s 1h
   expiry is irrelevant to inference). Refresh/write-back collisions with a
   running codex are therefore rare — but `cxswap` still handles them: on
   every switch *away*, the outgoing live file is re-stashed into its slot,
   so a token rotated by a running codex is never clobbered by an older
   snapshot. A live login that matches no slot is parked under `unclaimed/`
   instead of being destroyed (`cxswap unclaimed` lists them).

Account identity (email, plan, org, expiry) is parsed from the JWT claims —
no API calls are ever made.

## Commands

```
cxswap add                    # store the current codex login
cxswap list [--json]          # all accounts, active marker, token expiry
cxswap status [--json]        # which account is active right now
cxswap usage [--refresh] [--json]   # per-account rate limits (weekly + 5h)
cxswap auto [--threshold N] [--strategy best|next]   # watch & switch (below)
cxswap switch [NUM|EMAIL]     # rotate, or switch to a specific account
cxswap remove NUM|EMAIL       # forget a stored account
cxswap unclaimed              # parked unrecognized logins
cxswap import-cockpit         # import accounts from Cockpit Tools
cxswap purge [--yes]          # remove all codex-swap data
```

`--json` emits a single machine-readable object on stdout; notices go to
stderr. Payloads carry `schemaVersion: 1` (additive evolution, same contract
as claude-swap).

## Data layout

| What | Where |
|---|---|
| Codex login (untouched format) | `$CODEX_HOME/auth.json` |
| Slot metadata | `$CODEX_SWAP_BACKUP/sequence.json` |
| Slot credential copies | `$CODEX_SWAP_BACKUP/credentials/` |
| Parked unclaimed logins | `$CODEX_SWAP_BACKUP/unclaimed/` |

Removing or purging never touches the live `auth.json` — you stay logged in.

## Design notes

- **Zero runtime dependencies.** The job is one JSON file + JWT parsing we
  never verify; a credential-adjacent tool should have a tiny supply chain.
- **Atomic writes everywhere** (temp file → fsync → `os.replace` → dir
  fsync): a torn write would destroy the only refresh-token copy.
- **Refresh-token hash is lineage identity** — stable across the ~10-day
  access-token rotations, exactly like claude-swap's `credential_fingerprint`.
- **The live file is the source of truth.** Slot copies are refreshed from it
  on every switch away; stale snapshots are never installed over newer bytes.

## Usage dashboard

`cxswap usage` reads `GET https://chatgpt.com/backend-api/wham/usage` per
account — the same endpoint the Codex TUI's rate-limit bar uses (plain
headers, no client impersonation). Windows: an account-level weekly
budget plus per-model 5h/7d limits (`additional_rate_limits`).

Token policy per slot (the [openusage](https://github.com/robinebers/openusage)
#516 lesson, adapted — refresh tokens are single-use and the codex CLI
rotates the live file out-of-band):

- the **active** slot is measured with the live `auth.json`'s token; we
  never refresh the live file ourselves (the codex CLI owns it);
- **inactive** slots are measured with their stored copy, refreshed by us
  when the ~10-day access token has expired — safe because codex-swap is
  the sole writer of those files. A dead refresh token marks the row
  `auth-needed` instead of failing the report.

Results are cached per slot for 5 minutes (`--refresh` bypasses); a failed
fetch serves the last-good snapshot marked `stale` with its age.

## Automatic switching

```bash
cxswap auto                       # foreground loop, polls every 60s
cxswap auto --threshold 70        # switch earlier (default 80)
cxswap auto --strategy next       # rotate 1 -> 2 -> 3 instead of best-quota
cxswap auto --once                # single check, for cron
.cxswap auto --dry-run             # log what it would do, never switch
```

When the active account's **binding window** — the highest utilization
across its account-level and per-model windows — crosses the threshold, the
engine switches to a candidate below it, *before* the limit hits (a running
codex picks the new login up on its next message). Policy ported from
claude-swap's UI-agnostic engine: hysteresis margin stops two accounts
ping-ponging at the line, a 5-minute cooldown bounds the switch rate
(bypassed only when the active account is hard at its limit), usage comes
from the shared 5-minute cache so the poll loop stays cheap, and missing
usage means *hold*, never switch on a guess. The cooldown persists across
processes (`autoswitch_state.json`), so cron-driven `--once` ticks behave.

For cron, `--once` reports via its exit code (0 switched / 1 error /
2 nothing to do / 3 blocked) and `--json` emits one event per line:

```bash
*/5 * * * * cxswap auto --once --json >> ~/.cxswap-auto.log 2>&1
```

## Importing from Cockpit Tools

If you already manage Codex accounts with
[Cockpit Tools](https://www.antigravity.io/) (the `~/.antigravity_cockpit`
app), one command migrates them over:

```bash
uv tool install --editable '.[cockpit]'   # adds the decryption dependency
cxswap import-cockpit                      # decrypt the local store directly

# or, from Cockpit's UI (Accounts -> Export): no extra dependency needed,
# and the file can come from another machine
cxswap import-cockpit --file ~/Downloads/accounts.json
```

Accounts are decrypted locally (key file + AES-256-GCM, nothing leaves the
machine) and stored as slots. **Newest-token-wins**: an account cxswap
already knows is only updated when Cockpit's copy carries newer tokens, so
importing can never resurrect an already-rotated refresh token.

> Caveat: two managers sharing one account line means whichever refreshes a
> token first supersedes the other's copy. After you start using an account
> from cxswap (its token expires and gets rotated), Cockpit's stored copy of
> that account will fail with `refresh_token_reused` until you re-import or
> re-login there. One manager per account line is the steady state.

## Roadmap

- [x] Usage dashboard (`wham/usage`, weekly + 5h windows, per-model limits)
- [x] `cxswap auto` — threshold/hysteresis/cooldown engine (best + next strategies)
- [ ] `cxswap run N` — per-terminal account via per-profile `CODEX_HOME`
- [ ] `add-token` for long-lived setup tokens / API keys
- [ ] Export/import between machines

## Development

```bash
uv sync                 # dev deps (pytest, ruff) from the lockfile
uv run pytest           # 52 tests, fully isolated (env-var tmp homes)
uv run ruff check src tests && uv run ruff format --check src tests
uv run cxswap --help    # run from the checkout
```

No test touches real state: `CODEX_HOME` and `CODEX_SWAP_BACKUP` are
redirected per-test via env vars (the same knobs documented above), and
the HTTP layer is injectable (`opener=`) so no test hits the network.

### Iteration flow

- `main` is always green: CI runs lint + tests on Linux/macOS/Windows.
- Feature work happens on `feat/<topic>` branches, merged to `main` via
  PR (or fast-forward push for solo work) once CI is green.
- Releases: bump `version` in `pyproject.toml`, update `CHANGELOG.md`,
  commit as `release: vX.Y.Z`, tag `vX.Y.Z`, push the tag.
- Changes worth a changelog entry: anything user-visible (commands,
  flags, JSON schema, storage layout) — internal refactors don't need one.
- JSON payloads are additive-only (`schemaVersion: 1`): new fields may
  appear, existing fields never change meaning — scripts must ignore
  unknown keys.

## License

MIT
