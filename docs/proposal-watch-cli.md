# Proposal: `agent-wake watch`

**Status:** awaiting operator approval; no implementation yet.

## Recommended default

Add `agent-wake watch` as a read-only observer over the existing Unix socket.
Extend `hello` with `role: "watch"`; the daemon copies lifecycle events to watch
connections without registering them as delivery subscribers and without
requiring wake acknowledgements.

Authentication defaults to local same-user access: retain socket mode 0600 and
verify the peer UID where the platform exposes Unix peer credentials. Refuse
watch mode when peer identity cannot be established unless the operator has
configured a dedicated watch token. Never reuse ingress or callback HMAC keys.

Default output is NDJSON containing timestamp, event ID, source, kind,
destination, and delivery outcome. Content and metadata values are replaced by
lengths and key names. `--show-content` is an explicit opt-in that prints a
stderr warning; secret-like metadata keys remain redacted regardless. The
daemon should apply a bounded observer queue and disconnect a slow watcher
rather than delaying delivery.

Approval should confirm the peer-credential fallback and whether
`--show-content` is acceptable before BC-WAKE-021 moves to implementation.
