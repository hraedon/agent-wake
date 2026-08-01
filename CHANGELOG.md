# Changelog

All notable changes to agent-wake are documented here.

## Unreleased

### Added

- A shared `agent-waked` daemon that owns authenticated HTTP ingest, source
  routing, adapter subscriptions, and outbound replies.
- Durable SQLite-backed deduplication, next-session delivery, dead letters,
  retention controls, and queue-management CLI commands.
- Daemon-client adapters for Claude Code and opencode, including opencode
  per-session label routing and Claude deferred silent injection.
- Human-directed webhook and email delivery with principal allowlists.
- URI-addressed secret resolution, Vault support, secret lifecycle commands,
  and live daemon config reload.
- Suite-compatible `doctor` and `install-harness` commands, systemd and Windows
  service packaging, and container deployment support.
- A two-minute first-wake quickstart.

### Changed

- Renamed the coordination integration from substrate to regista.
- Moved HTTP ingest and callback delivery out of adapters and into the daemon.
- Made delivery semantics explicit per event: `live_only`, `next_session`, or
  `managed_session`.

### Fixed

- Doctor now reports unknown or uncovered live-only subscriber state instead
  of presenting a silently dropping daemon as healthy.
- Shutdown drains in-flight human deliveries and router acknowledgement
  waiters before closing durable state.
- Outbound delivery rejects unsafe redirects and preserves permanent failures
  in the dead-letter store.
- Identifier scanning, CLI conformance, malformed-config handling, adapter
  liveness, and secret-visibility health reporting were hardened.

## 0.0.1 - 2026-05-25

### Added

- Initial dogfoodable release with HMAC-gated HTTP wake ingest, Claude Code and
  opencode adapters, reply delivery, Claude permission relay, and in-memory
  event-id deduplication.
