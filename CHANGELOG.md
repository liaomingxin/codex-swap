# Changelog

All notable changes to codex-swap are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/liaomingxin/codex-swap/releases/tag/v0.1.0
