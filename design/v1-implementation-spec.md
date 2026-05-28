# Identity and multi-user — v1 implementation spec

**Status:** Consolidation of three review rounds. Ready to drive implementation
planning. Supersedes the open-questions framing of
[identity-and-multi-user.md](identity-and-multi-user.md); that document and
its appended review sections remain the historical record.

**Scope:** v1 regista changes, v1 agent-wake / agent-provenance schema
hooks, and the v2 roadmap items each v1 hook enables. Implementation tasks
should track against this document; design debate should not.

---

## 1. Locked positions

Three rounds of review reached consensus on the following. Each is a
decision, not a proposal. Any reopening requires concrete code or
use-case evidence and should be filed as a new breadcrumb.

### Identity model

- **Identity primitive lives in regista.** The mapping from identifier
  to real-world human is a deployment concern.
- **On-the-wire identifier is opaque.** Implementation uses URI-prefix
  encoding (`key:sha256:...`, `oidc:...`, `did:...`) carried in
  `principal_id`. The prefix is the attestation-type discriminator;
  regista's verification path will need a `PrincipalResolver`
  interface for non-`key:` prefixes (deferred to BC-196 implementation).
- **Identity is stable across key rotation.** `principal_id` does not
  change when the underlying key rotates. The binding lives in a chain
  of signed `key_rotation` events in the log. Not DIDs.

### Deployment model

- **Peer model first.** Hosted is a different project, out of scope.
- **Single-user is the degenerate case of multi-user.** No separate
  code path. On first run, identity is auto-generated; the multi-user
  machinery exists but is trivial when n=1.

### Trust and authorization

- **Trigger identity and actor identity are both first-class.**
  Recorded as separate fields on every provenance event. Identity is
  stamped out-of-band by the provenance recorder, not passed through
  the agent context. In-context identity metadata is advisory only.
- **Strict attribution for v1.** Every tool call attributed to the
  harness operator. The `on_behalf_of` delegation chain is
  schema-present but cryptographically self-attested until BC-196
  lands. *Note:* "strict attribution" means *operator's key* in v1
  and *the human who issued the session grant* in v2; document this
  distinction so the v2 implementation is unambiguous.
- **Per-user sender allowlists.** Cross-user consent is a signed event
  in the log.
- **Auditors use the same identity primitive with a role gate.**
  `role` field on `KeyEntry` (`actor` / `auditor` / `recovery`).
  Auditor keys can only sign attestation-class events; actor keys
  cannot sign attestations. Enforcement at `verify_key_status()`.

### Auditor access

- **Hybrid access model.** Direct verification for discovery (auditor
  selects events to look at during the audit), durable Merkle bundles
  with RFC 3161 timestamp anchors for evidence (auditor takes away an
  artifact that survives independent cross-examination). The bundle is
  the authoritative compliance artifact; direct verification is a
  development convenience.

### Key management

- **Local keygen, signed rotation, signed revocation.**
- **Revocation must carry a timestamp** (`revoked_at`). Without it,
  revocation kills verification of historical events too.
- **Second-device recovery key.** V1 ships with the schema hook
  (recovery key entry with `role: "recovery"`). V2 implements the
  rotation flow.
- **Recovery implementation may be v1, not v2.** Last-bite challenge
  from round 3 (Mimo). ~50 lines of code prevents permanent identity
  loss on key compromise; the workplace-audit use case does not
  tolerate "lose your laptop, lose your audit chain." Decision
  deferred to implementation planning.

### Temporal integrity

- **Add `timestamp` to the signing envelope now.** No production
  deployments; the window for backward-compat-free change is now.
- **Verifier enforces ordering:** `authenticated_at ≤ event.timestamp
  ≤ TSA.timestamp + tolerance`. Regista validates structure; the
  verifier validates semantic consistency.
- **RFC 3161 anchoring is configurable per deployment.** No hardcoded
  default TSA. Documented examples: FreeTSA, DigiCert public TSA.
  Batch interval configurable; reasonable default 15 minutes (round
  3 split between 5min / 15min / 1hr; 15min is the midpoint and the
  recommendation).

### Cross-cutting

- **Regista-less signing is supported via vendored schema.**
  `_signing.py` + `_jcs.py` extracted into a standalone library that
  agent-wake can depend on without pulling in all of regista. This
  is an implementation task, not a design question.
- **`alg` discriminator is fail-shut.** When BC-196 lands, unknown
  algorithm values cause verification to fail. Never warn-and-continue.

---

## 2. Regista change inventory

Each change is filed as a breadcrumb against regista; the BC number
is the authoritative tracking artifact. The table below is the
roll-up.

| # | What | File(s) | BC |
|---|---|---|---|
| 1 | Add `timestamp`, `key_id`, `event_seq`, `workflow_name`, `workflow_version` to signing envelope | `_signing.py:build_signing_envelope` | BC-214 |
| 2 | Add `revoked_at: str \| None` to `KeyEntry`; honor in `verify_key_status` for historical events | `_keys.py:KeyEntry`, `verify_key_status` | BC-215 |
| 3 | Add `public_key`, `alg` to `KeyEntry`; distinguish HMAC secrets from Ed25519 private key bytes; explicit base64 encoding for binary key material | `_keys.py:KeyEntry`, `_load` | BC-216 (depends on BC-196) |
| 4 | Per-actor / per-principal key resolution; allow multiple active keys with a `principal_id` binding | `_keys.py:KeySet`, `_event_store.py:append_event` (accept `key_id` override) | BC-217 (depends on BC-216) |
| 5 | Add `role: str = "actor"` to `KeyEntry`; enforce at sign-time policy gate | `_keys.py:KeyEntry`, `verify_key_status` | BC-218 |
| 6 | Validate `session_id` in `validate_delegation_chain`; add `expires_at` and `session_grant_event_id` to permitted `on_behalf_of` keys | `_contract.py:validate_delegation_chain` | BC-219 |
| 7 | Unify timestamp source between `InMemoryEventStore` and `PostgresEventStore`; sign client-generated timestamp; DB INSERT uses client timestamp explicitly | `_event_store.py:append_event` (both backends) | BC-220 |
| 8 | Reserve `checkpoint` event transition for log compaction; document semantics | `_types.py`, agent-provenance verifier | BC-221 |

Plus existing breadcrumbs that this work depends on or extends:

- **BC-196** (asymmetric signing, accepted, not implemented) — the
  load-bearing prerequisite for the auditor model. BC-214 / 216 / 217
  / 218 all reach their full security value only when BC-196 lands.
- **BC-197** (delegation chain, implemented) — the structural
  foundation for the session-grant topology. Cryptographic enforcement
  of the chain still waits on BC-196.
- **BC-198** (operator-forgery defense, accepted, not implemented) —
  Layer 1 (RFC 3161) is implementable today against HMAC and should
  land before BC-196. This implements Q12.

---

## 3. New event schemas

These are consumer-defined transition types that go through the
existing `sign_event()` path. Regista does not need to special-case
them; verifiers do. They are listed here so the agent-provenance
verifier and the agent-wake adapter can agree on the wire format.

### `scope_attestation`

Signed by the operator's identity key. Versioned via chained
references. The verifier walks the chain to know point-in-time scope.

```json
{
  "transition": "scope_attestation",
  "actor_id": "<operator>",
  "payload": {
    "scope_id": "<uuid>",
    "previous_scope_id": "<uuid|null>",
    "effective_from": "2026-05-23T14:00:00Z",
    "harnesses": [
      {"name": "claude-code", "version": "...", "config_digest": "sha256:..."}
    ],
    "out_of_scope": "free-form text describing exclusions"
  }
}
```

### `key_declaration` (bootstrap)

Signed by the declared key (self-referential). The first event in any
regista log used for audit. Documents what public key the operator
declared at initialization.

```json
{
  "transition": "key_declaration",
  "actor_id": "<operator>",
  "payload": {
    "principal_id": "key:sha256:...",
    "public_key_fingerprint": "sha256:...",
    "attestation_type": "key",
    "declared_at": "2026-05-23T00:00:00Z",
    "bootstrap_channel": "manual_ceremony | dns:... | github:... | local:self"
  }
}
```

For solo-dogfood: `bootstrap_channel: "local:self"`. The verifier
treats this as integrity-only ("the same person signed and verifies"),
not as authenticity ("an independent party verified the key
fingerprint"). Documentation contract: solo mode is adequate for
internal audit; arms-length audit requires a bootstrap channel the
operator does not control.

### `key_rotation`

Signed by the old key (or by a recovery key with `role: "recovery"`).
Links old key to new key, preserving identity continuity for the
stable `principal_id`.

```json
{
  "transition": "key_rotation",
  "actor_id": "<operator>",
  "payload": {
    "principal_id": "alice",
    "old_key_fingerprint": "sha256:...",
    "new_key_fingerprint": "sha256:...",
    "rotated_at": "2026-05-23T14:00:00Z",
    "rotation_reason": "scheduled | compromise | recovery"
  }
}
```

### `session_grant` (v2 implementation, v1 schema hook)

Signed by the human's identity key. Authorizes an agent's ephemeral
key to sign events on the human's behalf for a bounded scope and
lifetime.

```json
{
  "transition": "session_grant",
  "actor_id": "<human>",
  "payload": {
    "session_id": "<uuid>",
    "grantee_key_fingerprint": "sha256:...",
    "scope": ["edit", "read", "bash:safe-subset"],
    "valid_from": "2026-05-23T14:00:00Z",
    "valid_until": "2026-05-23T22:00:00Z"
  }
}
```

Action events reference the grant via `on_behalf_of.session_grant_event_id`
(new field, schema validated in BC-219). The verifier checks the
action event's signature against the agent's session key, then checks
the session grant's signature against the human's identity key. Two
signature chains, one signed envelope per event.

### `session_revocation` (v2)

Signed by the human's identity key. Invalidates a session grant
mid-session.

### `timestamp_anchor` (per BC-198)

Carries an RFC 3161 token covering the Merkle root of a batch of
events. Configurable batch interval (default 15 minutes).

### `heartbeat` (v1 schema, v2 enforcement)

Periodic event emitted by configured harnesses. The verifier checks
for gaps. V1 reserves the transition name; v2 implements the
enforcement (gap detection is harness-cooperation-dependent).

```json
{
  "transition": "heartbeat",
  "actor_id": "<harness or operator>",
  "actor_kind": "agent | system",
  "payload": {
    "harness_name": "claude-code",
    "session_id": "<uuid>",
    "uptime_seconds": 3600
  }
}
```

### `auditor_attestation`

Signed by an `auditor`-role key. Attests that an auditor reviewed a
range of events and reached a verdict. The attestation can travel
with an exported bundle (as a bundle co-signature) or live in the log;
the verifier accepts either location.

```json
{
  "transition": "auditor_attestation",
  "actor_id": "<auditor>",
  "payload": {
    "audit_range_start": "<event_id>",
    "audit_range_end": "<event_id>",
    "merkle_root": "sha256:...",
    "verdict": "passed | flagged",
    "notes": "free-form"
  }
}
```

### `checkpoint` (v1 schema reserve, v2 implementation)

A signed Merkle root covering the log up to a point in time, plus a
TSA token anchoring that root. Subsequent verifiers can start from
this checkpoint rather than replaying from genesis, enabling event
archival without breaking the verification chain.

---

## 4. Verifier tool contract

The verifier is an offline tool that consumes a regista log (live
or as a bundle) and produces a structured report. It is *not* part of
regista; it lives in `agent-provenance`. The contract below is what
regista's schema and `sign_event` path are designed to support.

### What the verifier checks

1. **Signature validity** for every event. Under HMAC, the verifier
   needs the secret. Under Ed25519 (BC-196), the verifier needs the
   actor's public key.
2. **Algorithm contract.** Unknown `alg` value → fail-shut.
3. **Key lifecycle.** For events signed by a `revoked` key, the
   verifier checks `revoked_at` and accepts events with
   `event.timestamp < revoked_at`. For `deprecated` keys, warn but
   accept.
4. **Role gate.** Event transitions that require `role: "actor"` are
   rejected if signed by a key with a different role. Same for
   `auditor` and `recovery`.
5. **Rotation chain integrity.** For every historical key, the
   verifier walks `key_rotation` events to confirm the chain is
   unbroken and each rotation is signed by its predecessor.
6. **Scope coverage.** For every tool-call event, the verifier finds
   the active `scope_attestation` (latest with `effective_from ≤
   event.timestamp`) and confirms the event's harness is listed in
   that scope.
7. **Heartbeat continuity** (v2). Gaps > 2× heartbeat interval are
   flagged as "possible event omission."
8. **Temporal ordering.** `authenticated_at ≤ event.timestamp ≤
   TSA.timestamp + tolerance`. Violations are warnings, not errors.
9. **Session grant validity** (v2). For action events with
   `session_grant_event_id`, the verifier resolves the grant, checks
   the grant's signature against the human's key, and confirms the
   action falls within the grant's scope and lifetime.
10. **Cross-bundle chain continuity.** When given multiple bundles,
    the verifier walks `manifest.json` `previous_bundle_hash` links
    and confirms each predecessor hash matches. Breaks in the chain
    are reported with the missing bundle ID range so the auditor knows
    exactly where evidence is absent. The first bundle in a chain
    (`previous_bundle_hash: null`) is reported as a chain-origin
    finding.

### Output

A structured report (JSON + human-readable) listing:
- Verified events (signature + metadata checks pass)
- Warnings (temporal ordering, deprecated keys, scope mismatches)
- Errors (signature fails, role violations, rotation chain breaks)
- Out-of-scope events (events from harnesses not in active scope)
- Heartbeat gaps (v2)

### Bundle format

```
bundle/
  events.jsonl              # signed events, one per line
  keys.json                 # public keys, role-tagged, with rotation history
  scope_attestations.json   # scope chain
  bootstrap.json            # initial key_declaration event
  timestamp_anchors/        # RFC 3161 tokens
  auditor_attestation.json  # optional: an auditor's signed verdict
  manifest.json             # bundle metadata, integrity hashes
```

The bundle is the unit of evidence. The verifier accepts a bundle
directory and produces a report. It does not require network access
(except optionally to verify TSA token chains against the TSA's CA).

#### `manifest.json` cross-bundle hash chain

`manifest.json` carries an integrity hash for every file in the bundle
*and* a back-pointer to the previous bundle. The cross-bundle chain
mirrors AWS CloudTrail's digest-file model (`previousDigestHashValue`
+ `previousDigestHashAlgorithm`) and allows the verifier to detect
gaps across retention boundaries: a missing or tampered bundle in the
sequence fails verification at the gap rather than being silently
skipped.

```json
{
  "bundle_id": "<uuid>",
  "bundle_range_start": "<event_id>",
  "bundle_range_end": "<event_id>",
  "created_at": "2026-05-23T14:00:00Z",
  "previous_bundle_hash": "sha256:...",
  "previous_bundle_hash_algorithm": "sha256",
  "previous_bundle_id": "<uuid|null>",
  "files": {
    "events.jsonl": {"sha256": "...", "size": 12345},
    "keys.json": {"sha256": "...", "size": 678},
    "...": "..."
  }
}
```

For the first bundle in a chain, `previous_bundle_hash` is `null` and
`previous_bundle_id` is `null`. The verifier reports a "chain origin"
finding so an auditor can confirm the first bundle is the expected
starting point.

*Prior art:* AWS CloudTrail log-file integrity validation — see
`research-findings.md` §2.1.

---

## 5. V2 roadmap

Items deferred from v1 with their v1 hooks identified:

| V2 item | V1 hook | Depends on |
|---|---|---|
| Asymmetric signing (Ed25519 actor keys, public key auditors) | BC-214 (envelope), BC-216 (KeyEntry alg field) | BC-196 |
| Session grant cryptographic enforcement | BC-219 (`session_grant_event_id` schema), `session_grant` event type reservation | BC-196, BC-216 |
| Auditor cryptographic separation | BC-218 (role field), `auditor_attestation` event type | BC-196 |
| Harness heartbeat enforcement | `heartbeat` event type reservation | session_grant (so heartbeats have their own key, not the operator's) |
| Multi-device users (one principal, multiple devices) | BC-217 (per-principal key resolution) | BC-216 |
| Log compaction / checkpoints | `checkpoint` event type reservation | BC-221 |
| Recovery key implementation | BC-218 (role field, with `recovery` value) | None — could ship v1 (Mimo's last bite) |
| GDPR redaction-by-hash | agent-provenance/README §7 already sketches the schema | None — implementation in agent-provenance, not regista |
| Cross-org delegation (scenario 3) | Existing `on_behalf_of` and `key_declaration` mechanisms | Bootstrap channel via DNS / GitHub / OIDC |

### Items still open (not blocking v1)

- HMAC-to-Ed25519 transition strategy (GLM's round-3 question). When
  do you deprecate HMAC? Coexistence is supported by the `alg`
  discriminator, but the transition plan needs documentation.
- Multi-device users (Q15, Kimi/Gemini). Real concern, but solvable
  after BC-217 lands.
- Event log compaction / GDPR erasure (Q16/Q17). Schema reserved via
  `checkpoint` event type; full design is its own work item.

---

## 6. Implementation sequencing

Suggested order of work, optimized for landing the v1 audit-credible
configuration as fast as possible without locking out v2.

1. **BC-220** (timestamp source unification) and **BC-214** (envelope
   gaps). These are the same code change and have to land together.
2. **BC-215** (`revoked_at`). Small, independent, unblocks proper key
   lifecycle.
3. **BC-218** (role field on KeyEntry). Small, enables Q6 enforcement.
4. **BC-219** (`session_id` and session-grant-related validation).
   Small, unblocks v2 session-grant work without implementing it.
5. **BC-221** (`checkpoint` event reservation). Documentation /
   schema only; defers actual compaction.
6. **BC-198 Layer 1** (RFC 3161 timestamping). Independent of BC-196;
   can ship now. Implements Q12's external anchoring.
7. **Verifier tool v1** (agent-provenance side). Consumes the
   regista log, applies the contract above, produces a report.
8. **BC-196** (asymmetric signing). The big one. Unlocks the full
   auditor model.
9. **BC-216** + **BC-217** (key model overhaul). Lands with or
   immediately after BC-196.
10. **Session grant v2 implementation** (agent-wake side). Uses
    BC-219's schema and BC-216/217's key model.

Steps 1–5 are pure regista work, ~all small. Step 6 is an
integration. Step 7 is consumer-side. Steps 8–10 are the v2 build.

---

## 7. What is *not* in this spec

- Hosted multi-tenant deployment (out of scope, different project).
- Cross-org delegation as a primary use case (v2+).
- Full GDPR compliance (deployment concern beyond redaction-by-hash).
- Replacement of regista's HMAC default (BC-196's non-goal is
  explicit: HMAC stays as the zero-config homelab default
  indefinitely).
- Channels protocol details for the harness-adapter side (lives in
  agent-wake's separate implementation work, not this identity spec).

---

## 8. Open implementation questions

These remain open but are not design questions — they are
implementation choices to make when filing the PR.

- **Recovery in v1 or v2?** Mimo's round-3 argument (50 lines of
  code, prevents permanent identity loss) is compelling for the
  workplace-audit use case. Recommend v1 *if* it doesn't slow the
  rest of the work.
- **TSA batch interval default.** Round 3 split between 5 / 15 / 60
  minutes. Recommend 15 minutes — fine enough for workplace audit,
  cheap enough for public TSAs.
- **Bundle storage format.** JSON Lines vs. tar vs. zip. Recommend
  JSON Lines (newline-delimited JSON) for streamability and
  human-readability.
- **`role` field default value.** Recommend `"actor"` (the most
  common case, backward-compatible with existing key files).
