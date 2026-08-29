# Changelog

All notable changes to codex-swap are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-08-29

### Added

- `cxswap import-cockpit [--path DIR] [--file JSON] [--json]`: import Codex
  accounts from Cockpit Tools — either the local encrypted store
  (AES-256-GCM with the key file, needs the `cockpit` extra) or a
  plaintext UI export file (`--file`, zero extra deps, cross-machine).
  Newest-token-wins: a slot's credentials are never downgraded to an
  older copy.
- Redesigned `cxswap usage` human view: per-account cards with
  color-graded unicode usage bars (green <50 / yellow <80 / red >=80),
  relative + absolute reset times, per-model limit rows, LIMIT REACHED
  markers, cached-age notes. TTY-gated color (`NO_COLOR`,
  `CLICOLOR_FORCE=1`); `--json` output unchanged.
- Richer usage detail, split by cost:
  - free (same response): credits balance / approx local+cloud messages
    remaining, rate-limit reset credits (available vs applicable),
    code-review rate-limit window;
  - free (stored credential): token validity + days left, last refresh,
    subscription until;
  - `--stats` (one extra request per account, `/profiles/me`): display
    name, lifetime tokens, peak-day tokens, streaks, thread count,
    most-used reasoning effort.

### Fixed

- Identity parsing: JWT claims are now read from both the AUTH and
  PROFILE namespaces (an `or`-chain shadowed the second when both were
  present, e.g. emails in access tokens).
- `UsageSnapshot.to_json` dropped the supplementary fields on the cache
  roundtrip (parse worked, serialization didn't) — now covered by a
  roundtrip test.

## [0.1.0] — 2026-08-29

Initial release.

### Added

- **Account management**: `cxswap add / list / status / remove / purge`,
  slots with stable numbers, email/plan parsed from JWT claims (no API
  calls), `--json` contract (`schemaVersion: 1`) on every read command.
- **Manual switching**: `cxswap switch [NUM|EMAIL]` with rotation, no-op
  self-switch detection, and switch-away stashing — the live `auth.json`
  is always re-captured into its slot before a swap, so tokens rotated by
  a running codex are never clobbered by stale snapshots. Unrecognized
  live logins are parked under `unclaimed/` instead of destroyed.
- **Usage dashboard**: `cxswap usage [--refresh] [--json]` against
  `chatgpt.com/backend-api/wham/usage` (weekly + 5h windows, per-model
  limits). Active slot measured with the live token; inactive slots
  refreshed in place when their ~10-day access token expired (sole-writer,
  race-free). 5-minute cache, stale-on-error with age notes.
- **Auto-switch engine**: `cxswap auto` — threshold/hysteresis/cooldown
  policy ported from claude-swap, `best` (most quota left) and `next`
  (1→2→3 rotation) strategies, `--once` for cron with claude-swap-parity
  exit codes, `--json` JSONL event stream, cross-process cooldown state.
- Atomic durable writes everywhere (temp → fsync → `os.replace` → dir
  fsync); zero runtime dependencies; 52 tests.

[0.2.0]: https://github.com/liaomingxin/codex-swap/releases/tag/v0.2.0
[0.1.0]: https://github.com/liaomingxin/codex-swap/releases/tag/v0.1.0
