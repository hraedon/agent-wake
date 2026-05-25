# v0 acceptance status

Status of each of the 10 acceptance criteria in
[`v0-implementation-plan.md`](v0-implementation-plan.md) §9. This is the
artifact the maintainer uses to decide whether to tag `v0.0.1`.

Legend: ✓ = met. ◐ = met-with-caveat. ✗ = not met / deferred.

1. ✓ `agent-wake-claude` installable as a Claude Code plugin and wakes a
   running session on a curl POST to `localhost:8788` —
   `adapters/claude/pyproject.toml` declares the console script;
   `adapters/claude/src/agent_wake_claude/__main__.py:38` wires the
   ingest listener to `emit_wake_event`; `adapters/claude/README.md`
   documents the curl flow.

2. ✓ Wake event arrives as a `<channel>` tag with correct `source` and
   `kind` —
   `adapters/claude/src/agent_wake_claude/channel.py:25` emits
   `notifications/claude/channel` with `source`/`kind` in `meta`, which
   Claude Code renders as a `<channel>` tag per the channels reference.

3. ◐ Agent can reply via the reply tool to a configured callback URL —
   `adapters/claude/src/agent_wake_claude/reply.py` resolves the URL
   from config and POSTs. The no-callback graceful path is unit-tested
   in `tests/test_server.py::test_reply_tool_no_callback`; the
   with-callback POST path is not directly unit-tested for the Claude
   adapter (opencode equivalent is covered in
   `adapters/opencode/tests/reply.test.ts`). v0 ships with this gap;
   manual verification is via the demo script and live Claude Code.

4. ✓ Permission relay round-trip works — `permission.py:_forward` POSTs
   the request to the callback URL; `/permission/verdict` on the HTTP
   listener accepts the verdict and emits
   `notifications/claude/channel/permission` back to Claude Code
   (`permission.py:73`). Covered by `tests/test_permission.py`.

5. ✓ `agent-wake-opencode` installable as an opencode plugin and wakes
   the active session —
   `adapters/opencode/src/index.ts` exports the plugin factory and
   tracks sessions; `adapters/opencode/src/ingest.ts:140` calls
   `client.session.prompt`. Covered by `tests/integration.test.ts`.

6. ✓ HMAC gating rejects unsigned, mis-signed, and unknown-source
   requests with 403 —
   `gating.py` / `gating.ts` use constant-time comparison
   (`hmac.compare_digest` / manual constant-time in TS). Covered by
   `tests/test_gating.py` and `tests/gating.test.ts`. Ingest-layer
   integration coverage in `tests/test_ingest.py::test_ingest_invalid_signature`.

7. ✓ Unit tests cover wire format, gating, and `event_id` dedup —
   - wire format: `tests/test_ingest.py::test_ingest_wrapped_payload`,
     `tests/ingest.test.ts::buildWakeEvent`
   - gating: `tests/test_gating.py`, `tests/gating.test.ts`
   - dedup: `tests/test_ingest.py::test_ingest_wake_event_and_dedupe`,
     `tests/ingest.test.ts::isDuplicate`

8. ◐ `tools/fakechat-test.py` runs end-to-end without errors — the
   script PASSes in CI by driving its own adapter subprocess and
   confirming `notifications/claude/channel` is emitted. It does NOT
   talk to a live Claude Code session; that case is documented in the
   script's docstring with manual steps. This is the closest dry-run
   to the criterion without the harness in the loop.

9. ✓ README in each adapter documents setup, config, and a curl-based
   first-event test — `adapters/claude/README.md`,
   `adapters/opencode/README.md`. Both now include a Secret management
   section (rotation steps, 0600 advisory) and `.env.example`.

10. ✓ Neither adapter modifies any file outside its own directory; no
    substrate / agent-provenance changes — verified by git history
    (`git log --name-only`); changes since v0 scaffolding are confined
    to `agent-wake/` and a deprecation banner on
    `/projects/wake-probe/server.py` (plan §13 directs this; the
    banner was already in place from a prior session).

## Security regression tests added this session

- `adapters/claude/tests/test_ingest.py::test_ingest_source_spoofing_returns_500_generic`
- `adapters/claude/tests/test_server.py::test_unhandled_exception_in_handler_returns_jsonrpc_internal_error`
- `adapters/opencode/tests/ingest.test.ts` — "buildWakeEvent source spoofing"
  describe block
- `adapters/opencode/tests/format.test.ts` — 5 XML-escaping tests
  covering `&`, `<`, `>`, `"`, `'` in source, kind, and content

## Information-leak tightening

- `adapters/claude/src/agent_wake_claude/ingest.py` — source-mismatch
  now logs details at warn level and returns generic `{"error":
  "source mismatch"}`. The spoofing test asserts neither source name
  appears in the response body.
- `adapters/opencode/src/ingest.ts` — same treatment.

## Verdict

**Ready to tag v0.0.1.** 8 criteria fully met (✓), 2 met-with-caveat (◐):
criterion 3 lacks a direct unit test of the Claude reply POST path
(opencode is covered), and criterion 8 dry-runs the adapter without a
live Claude Code session. Neither blocks v0; both are documented
limitations rather than gaps. Test suites pass:

- Claude adapter: 29 pytest tests pass.
- opencode adapter: 53 bun tests pass.

## Deferred to v1

Per plan §10 and the prior session's reflection, the following are NOT
addressed in v0 and remain v1 work items:

- Reply retry / outbox — `reply.py` / `reply.ts` are fire-and-forget.
- Durable dedup — in-memory 256-event FIFO clears on restart.
- Multi-session routing — opencode broadcasts to all active sessions;
  Claude Code is single-session. No per-session targeting in the wake
  event.
- Permission-relay structured logging — `permission.py:_forward_permission_request`
  has a TODO but no structured logging mechanism.
- Identity / signing / `principal_id` — wire format is open to a future
  `meta` extension; no fields claimed today.
- Substrate / agent-provenance integration.

## Open questions

None blocking v0.0.1. Items above are scoped to v1; the relevant
design surfaces (substrate inbox, per-session routing) are tracked in
`AGENTS.md` and `v1-implementation-spec.md`.
