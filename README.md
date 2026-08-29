# codex-swap

Multi-account switcher for the [OpenAI Codex CLI](https://github.com/openai/codex).
Store several ChatGPT logins, switch between them with one command — no
browser re-login, no token juggling.

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
cxswap switch [NUM|EMAIL]     # rotate, or switch to a specific account
cxswap remove NUM|EMAIL       # forget a stored account
cxswap unclaimed              # parked unrecognized logins
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

## Roadmap

- [ ] `cxswap run N` — per-terminal account via per-profile `CODEX_HOME`
- [ ] Usage dashboard (`chatgpt.com/backend-api/api/codex/usage`)
- [ ] `cxswap auto` — threshold-based auto switching (engine design ports
      from claude-swap's UI-agnostic `autoswitch.py`)
- [ ] `add-token` for long-lived setup tokens / API keys
- [ ] Export/import between machines

## Development

```bash
uv sync
uv run pytest
uv run cxswap --help
```

## License

MIT
