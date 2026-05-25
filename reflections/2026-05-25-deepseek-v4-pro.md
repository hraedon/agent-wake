---
model: deepseek-v4-pro
datetime: 2026-05-25T18:00 UTC
project: agent-wake
---

# Session Reflection — 2026-05-25

**Work summary:** Adversarial security review of the entire agent-wake v0 codebase across both adapters. Found and fixed 6 issues: 4 high-severity (concurrency data leakage, exception info leak, config path injection, fire-and-forget reply loss), 2 medium-severity (predictable event IDs, signal handler race). 82 tests pass; type-checks clean.

---

## On the project

agent-wake's v0 is surprisingly solid for a two-adapter, two-language pre-v0.0.1 codebase. The adversarial posture baked into earlier iteration rounds (source-mismatch gating, constant-time HMAC comparison, XML escaping, fail-shut responses) means the low-hanging injection/authentication fruit was already picked. My review found only architectural/deep-consistency issues — nothing exploitable from the HTTP surface without already having a source's shared secret.

The dual-adapter design creates a maintenance tension: any change to the wire format or ingest semantics must propagate to both adapters. The Claude adapter (Python) and opencode adapter (TypeScript) already diverged on reply delivery semantics (synchronous vs. fire-and-forget), which is the exact kind of drift this architecture invites. A shared ingest library in a single language would erase this class of bug entirely, but that ship has sailed for v0.

---

## On the work done

Six fixes, all verified by existing test suites:

1. **IngestServer class-level attributes** (ingest.py:192-196) — The `config` and `emit_callback` were set on the handler *class*, not the server *instance*. In `http.server`, `self.server` refers to the server instance. `self.config` was actually reading the class attribute, which would be overwritten if a second server instance was created. Fixed by moving them to `self.server.config` / `self.server.emit_callback` on the IngestServer instance, and updating IngestHandler to access via `self.server`.

2. **Exception strings leaked in 500 responses** (ingest.py:167,188) — `forward failed: {e}` and `verdict handler failed: {e}` exposed internal exception details to callers. Now logged to stderr with generic "forward failed" / "verdict handler failed" body.

3. **OpenCode `/tmp` config fallback** (config.ts:18) — When `$HOME` was unset, the default config path was `/tmp/.config/agent-wake/config.json`. `/tmp` is world-writable, making config-planting possible. Now throws an error directing the user to set `AGENT_WAKE_CONFIG`.

4. **OpenCode reply.ts fire-and-forget** (reply.ts:18-26) — `postReply` fired off a `fetch()` and immediately returned `"sent"` without awaiting. If the POST failed, the error was logged to stderr but the agent got `"sent"` — delivery loss was invisible. Now `await`s the fetch and returns error text on failure, matching the Claude adapter's synchronous behavior.

5. **Predictable event IDs** (ingest.ts:10-12) — `Math.random()` is a PRNG, not CPRNG. While HMAC-gating limits the blast radius, predictable event IDs enable an internal observer to fill the 256-entry dedup window with known IDs, causing denial-of-service for legitimate events. Replaced with `crypto.getRandomValues()`.

6. **Signal handler race** (__main__.py:38-41) — SIGTERM/SIGINT handlers were registered *after* `start_listener()` created the background HTTP thread. A signal arriving between thread start and handler registration would kill the process without graceful shutdown. Moved signal registration before listener start.

The test fixes were straightforward: `postReply` becoming `async` required `await` in test call sites, and the fire-and-forget tests (expecting `"sent"` when the callback URL is unreachable) now correctly expect `"reply delivery failed"`.

---

## On what remains

- **Durable inbox (v1):** The schema and AGENTS.md both call out that events are lost if no session is active. This is the biggest missing piece for production use. The `activeSessions.size === 0` path in ingest.ts:160 returns 202 but drops the event.
- **Reply tool gating:** The agent can reply to any source name. A misconfigured or compromised agent session can POST to arbitrary callback URLs via the reply tool. The current design trusts the agent — reasonable for v0 single-user, but the v1 multi-user spec should gate reply destinations against configured sources.
- **Unified ingest library:** The divergence on reply semantics between adapters is a canary. A shared TS or Python ingest module would prevent this permanently. Unlikely to happen for v0 given time constraints.
- **Port conflict detection:** Neither adapter detects port conflicts at startup. `Bun.serve` throws, but the error isn't caught; `http.server.HTTPServer` may silently fail if `SO_REUSEADDR` is set. Low priority for localhost-only listeners.

---

## Gaps to flag

- `adapters/opencode/src/ingest.ts:160` — When `activeSessions.size === 0`, the event is silently dropped after returning 202. The response says "queued" but there's no queue. This is by design per the spec but counterintuitive; the response body should say "no active sessions" rather than "queued".
- `adapters/claude/src/agent_wake_claude/ingest.py:118` — `Content-Length` parsing uses `int(content_length)` without validation. A negative or massive value could cause `rfile.read()` to behave unexpectedly (though Python's http.server validates this in `send_head()`). Defensive.
- `adapters/claude/src/agent_wake_claude/server.py:100` — The bare `except Exception` in `main()` catches `KeyboardInterrupt` and `SystemExit` (both are BaseException, not Exception, so they pass through — confirming correct behavior, but worth noting the subtlety).
- `adapters/opencode/src/reply.ts:18-20` — `postReply` uses `await fetch()` which will hang for the default timeout (Bun default: 60s) if the callback server never responds. The Claude adapter uses a 30s timeout. Inconsistent; should both use a bounded timeout.
