# Identity and multi-user — Round 3 (final) review

**Reviewer:** Qwen3.6-27b
**Date:** 2026-05-23
**Status:** Final round -- positions written for implementation, not further debate.

---

## 1. Pre-reading attestation

| File | Most surprising finding |
|------|------------------------|
| `identity-and-multi-user.md` | The "Agent positions" section (line 414) argues for raw key fingerprints while the "Proposed positions" (line 292) converges on opaque ID + attestation-type. This contradiction between two sections of the same document was flagged by 6/7 round-2 reviewers and remains unresolved. |
| `round-2-response.md` (Claude) | The session-identity observation (section 7a) is the most consequential gap identified: `DelegationChain.session_id` at `_types.py:67` exists but is unused, and no design question addresses how wake events route to sessions. This is orthogonal to principal identity. |
| `identity-review-round2-mimo.md` | MiMo correctly notes that `build_signing_envelope` at `_signing.py:10-26` excludes both `key_id` and `timestamp` from the signed payload. The key that signed is not integrity-protected. This is worse than any prior review stated. |
| `identity-and-multi-user-review-deepseek-v4-pro.md` | Deepseek's observation that BC-198 Layer 1 (RFC 3161) is implementable today against HMAC (line 189-190) changes the priority ordering: temporal integrity does not wait for BC-196. |
| `review-kimi-k2.6-turbo.md` | **The strongest single observation across all reviews.** Section 7.1 identifies the agent-key vs human-key topology gap: under HMAC, `on_behalf_of` is structurally present but cryptographically meaningless because anyone with the key can forge any `principal_id`. This is not a future problem -- it's the current state. |
| `review-qwen3.6-27b-identity-multi-user.md` | (Prior round-2 review by this model.) The key finding remains: BC-196 is the actual blocker, not identity format. With HMAC, the auditor model cannot work. |
| `identity-and-multi-user-gemini-3.5-flash.md` | Gemini's URI-prefix encoding proposal (`key:sha256:...`, `oidc:...`) is the most practical implementation of the opaque-ID consensus. It requires zero schema change since `principal_id` at `_contract.py:590` is already a bare string. |
| `identity-and-multi-user-glm-5.1.md` | GLM's observation about identity continuity across rotation (section 7, "Missing question: Identity migration and namespace stability") is the one honest argument for DIDs that no reviewer engaged with. |
| `_signing.py` | `verify_event` (line 93-98) retries with `on_behalf_of=None` for backward compat. This pattern will not scale as the envelope grows. Each new field adds a retry branch. |
| `_keys.py` | `KeySet.active_key()` (line 149) returns exactly one key. No concept of per-actor keys. The model is "one keyring, one active key, everyone signs with it." This is fundamentally incompatible with multi-user until BC-196 lands. |
| `_types.py` | `ActorKind` enum (line 13-16) has `AGENT`, `HUMAN`, `SYSTEM` -- a role taxonomy the design doc never references. `DelegationChain` (line 65-88) has `session_id` -- a session concept no design question addresses. |
| `_event_store.py` | The timestamp is set at line 88 (`datetime.now(UTC)`) AFTER the signature is computed at line 73-81. The signing envelope at `_signing.py:10-26` does not include `timestamp`. This is a hard temporal-integrity gap. |
| `agent-provenance/README.md` | Section 2 (line 46-51) makes scope attestation non-negotiable for v1. Section 4 (line 80-97) orders trust layers with RFC 3161 (layer 3) before asymmetric signing (layer 4), meaning temporal integrity can ship before BC-196. |

### Cross-reviewer convergence patterns

- **5/7 reviewers** flagged scope attestation (Q10) as a structural gap the design doc missed entirely.
- **5/7 reviewers** flagged the timestamp-not-in-envelope problem (Q12).
- **4/7 reviewers** flagged the trust-root bootstrap problem (Q13).
- **3/7 reviewers** flagged identity continuity across rotation (Q14).
- **1 reviewer** (Kimi) identified the agent-key vs human-key topology (Q11) -- this was unique and is the most consequential finding.
- **6/7 reviewers** confirmed the opaque-ID + attestation-type consensus (Q2).
- **Q6 split evenly:** 3 for original consensus (auditors are identities), 3 for dissent (separate primitive or role field).

---

## 2. Substrate code findings beyond prior reviewers

### Finding 1: `PostgresEventStore.append` uses DB clock, not application clock

`_event_store.py:323` -- The Postgres store returns `row["timestamp"]` from the INSERT, which is the database server's `now()`. This differs from the InMemory store at line 88 which uses `datetime.now(UTC)` in the application process. For temporal integrity, this means the timestamp authority depends on which store implementation is active. An operator running the application on a machine with a manipulated clock gets one timestamp; the DB server gets another. The signature covers neither.

### Finding 2: `KeySet` has no per-actor key routing

`_keys.py:149-164` -- `active_key()` returns the single active key. `verify_key_status()` (line 166) validates any key by ID. There is no mapping from `actor_id` to which key that actor should use. The current model is: one shared keyring, one active key, all actors sign with it. This means the `actor_id` in the signing envelope (`_signing.py:21`) is purely conventional -- anyone with the key can claim any identity. Prior reviewers noted this is self-attested under HMAC, but the code-level finding is stronger: there is no mechanism to even attempt per-actor key assignment. BC-196 must include not just asymmetric signing but also per-actor key resolution.

### Finding 3: `Event` dataclass carries `key_id` but signing envelope does not

`_types.py:99` has `key_id: str` on the `Event`. But `_signing.py:10-26` does not include `key_id` in the canonical envelope. This means the `key_id` stored on the event is not integrity-protected by the signature. A post-hoc modification of `key_id` would not be detected by signature verification. This matters for multi-user: if the verifier needs to know which key signed an event, and that information is outside the signature, the verifier cannot trust it.

### Finding 4: `ActorMetadata.role` is a free-form string with no validation

`_types.py:21` -- `role: str | None` is optional and unconstrained. This is the field that would carry `"auditor"` vs `"operator"` distinctions. Currently, nothing validates it. The `validate_delegation_chain` function at `_contract.py:582` validates `on_behalf_of` structure but says nothing about `ActorMetadata`. This means the role field is advisory only -- useful for Q6's middle path but requires explicit validation logic to be load-bearing.

---

## 3. Q6 (reopened): Auditors

**Position: (c) Middle path -- `role` on `KeyEntry`.**

### Why not (a) original consensus

The original consensus says "auditors are just another identity with the same primitive." The code already supports this: `_signing.py:sign_event()` takes `actor_id` and `on_behalf_of`, and an auditor's attestation event would go through the same path. GLM-5.1 made this argument cleanly: the code does not distinguish auditor from actor.

But the threat models differ materially:

- **Actor key compromise:** attacker forges tool-call events. The auditor sees anomalous actions in the log. Detectable.
- **Auditor key compromise:** attacker forges attestation events saying "this log is clean." The compromised verification layer hides the forged actions. Undetectable by downstream consumers.

The original consensus acknowledges auditor attestations are signed events in the log (line 350-354), but this creates a problem: if the auditor writes to the same log, they need write access. The settled position on auditor access (Question B, hybrid model) gives auditors read-only access. An auditor who signs an attestation "in the log" needs to write to the log, which contradicts read-only access.

This is not a theoretical problem. `agent-provenance/README.md` §6 draws the verifier as an offline tool. An offline verifier cannot write to the live log. The auditor's attestation must be an external artifact.

### Why not (b) Kimi/GLM dissent (separate primitive)

Kimi's proposal: "actor identity = signing key for log events; auditor access = separate token / public-key allowlist; auditor attestation = signed artifact stored as a bundle co-signature, not an event in the live log."

This is architecturally clean but over-splits. The auditor still needs a keypair. The auditor still needs key rotation. The auditor still needs the same key lifecycle states (`active`/`deprecated`/`revoked`). Creating a separate primitive for "auditor keyring" duplicates the `KeyEntry` dataclass, the `KeySet` class, and the verification path. At `_keys.py:17`, the `KeyEntry` is three fields. A separate auditor keyring would be the same three fields with different semantics. This is the kind of duplication that creates drift.

### Why (c) `role` on `KeyEntry`

Add `role: str = "actor"` to `KeyEntry` at `_keys.py:17`:

```python
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
    role: str = "actor"  # "actor" | "auditor" | "system"
```

This is a one-field addition. It enables:

1. **Policy enforcement:** `verify_key_status()` at `_keys.py:166` can check role. An auditor key signing a `tool_call` event is rejected. An actor key signing an `attestation` event is rejected.
2. **Unified key management:** rotation, revocation, and hot-reload work identically. No second keyring.
3. **Auditor attestations as external artifacts:** The auditor signs a Merkle bundle (or a scope attestation) with their key. The attestation is stored alongside the bundle, not in the live log. The verifier checks the attestation signature against the auditor's public key (resolved via the same registry, filtered by `role: "auditor"`).
4. **Composes with `ActorKind`:** `_types.py:13` already has `AGENT`, `HUMAN`, `SYSTEM`. The `role` on `KeyEntry` is independent -- it's about what the key is authorized to sign, not what kind of entity holds the key.

### Auditor attestation storage

The attestation is NOT a signed event in the live log. It is a signed artifact attached to the audit bundle. The bundle format:

```
bundle.json:
  events: [ ... ]
  merkle_root: "sha256:..."
  scope_attestation: { signed by operator key }
  auditor_attestation: { signed by auditor key, references merkle_root }
```

This resolves the contradiction: the auditor has read-only access to the live log, but can produce signed attestations that travel with the exported bundle. The verifier checks both signatures independently.

---

## 4. New Q10: Scope attestation

### Who signs

The **harness operator** signs the scope statement. Not a quorum, not an external party. The operator is the entity that configured the harnesses and is making the claim about what's in scope. This is consistent with the CloudTrail analog: AWS signs the log-file integrity digests. The operator signs the scope.

The scope statement is a signed event in the substrate log, using the operator's key. It has a special `transition` value (e.g., `"scope_attestation"`) to distinguish it from operational events.

### Schema

```json
{
  "type": "scope_attestation",
  "version": 1,
  "effective_from": "2026-05-23T00:00:00Z",
  "harnesses": [
    {
      "name": "claude-code",
      "version": "2.1.80",
      "config_digest": "sha256:abc123..."
    },
    {
      "name": "opencode",
      "version": "0.3.1",
      "config_digest": "sha256:def456..."
    }
  ],
  "out_of_scope": [
    "Any agent harness not listed above",
    "Direct model API calls outside configured harnesses"
  ]
}
```

The `config_digest` is load-bearing: `agent-provenance/README.md` §7 already includes it in the per-tool-call event schema. An auditor can verify that the hooks were configured at action time by comparing the per-event `config_digest` to the scope attestation's `config_digest`.

### Versioning

Scope is versioned. Each change produces a new scope attestation event. The `effective_from` timestamp establishes point-in-time scope. An auditor verifying events from `[T1, T2]` finds all scope attestations with `effective_from <= T2` and uses the one with the latest `effective_from <= event_timestamp`.

This is a new event type, not a scope-rotation event. The analogy to key rotation is superficial: key rotation preserves identity continuity; scope evolution does not preserve scope continuity. A narrower scope at T1 and a wider scope at T2 are not the same claim. Each is independently signed and independently verifiable.

### Harness heartbeats

**Not in scope for v1.** Heartbeats address the completeness problem (detecting when the operator routed around the harness). `agent-provenance/README.md` §2 already names this as an honest gap: "out of scope: preventing or detecting agent activity that does not flow through a configured harness." Heartbeats are a v2 feature that requires harness cooperation (the harness must emit periodic events even when idle). The scope attestation establishes what the claim is; heartbeats would provide evidence the claim is being honored. v1 ships the claim; v2 ships the evidence.

However, the schema should reserve a `heartbeat` transition type so the v2 implementation doesn't require a schema change.

---

## 5. New Q11: Agent-key vs. human-key topology

This is the highest-priority new question. Kimi identified it; no other reviewer matched the analysis.

### The problem, grounded in code

`_signing.py:43-57` -- `sign_event()` takes one `key: bytes`. Currently, this is the HMAC secret. Under the current design, both the human operator and the agent harness sign with the same key. The `on_behalf_of` field at `_signing.py:16` carries `principal_id` (the human's identity), but under HMAC, anyone with the key can set any `principal_id`. The delegation chain is structurally present but cryptographically meaningless.

Even with BC-196 (asymmetric signing), if agent and human share one keypair, the same problem persists: the signature proves "the holder of this key signed this event" but doesn't distinguish "the human signed" from "the agent signed on the human's behalf."

### The proposed topology

```
Human identity key (long-lived, ~/.config/)
  |
  |-- signs --> Session grant (delegation certificate)
                  |
                  +--> "Agent key 0xAgent... may sign on my behalf
                        for session S, scope X, duration N hours"
                        |
                        +--> Agent session key (ephemeral, per-session)
                                |
                                +--> signs --> Tool-call events
```

Two cryptographic links:
1. Event signature verified against agent's session key
2. Session grant signature verified against human's identity key

Two separate compromise blast radii:
- Agent key compromised: attacker can forge events for one session, for the session's duration and scope. Human key is safe.
- Human key compromised: attacker can issue session grants to any agent key. Full compromise.

### Should v1 support this topology?

**No. V1 should design the schema to permit it, but not implement it.**

Reasons:

1. **BC-196 is the prerequisite.** `_signing.py:sign_event()` returns one signature. Two-key verification requires either (a) the session grant as a separate signed event, or (b) the event envelope carrying both signatures. Both require changes to the signing interface that don't make sense until BC-196 lands.

2. **`KeySet` doesn't support per-actor keys.** `_keys.py:149` returns one active key. The session grant topology requires the verifier to resolve two keys per event: the agent's key (for the event) and the human's key (for the grant). This is a fundamental change to key resolution.

3. **The driving use case is solo + auditors.** In solo mode, the human and the agent are the same person on the same machine. The blast-radius separation is valuable but not urgent. The auditor needs to know "this event was signed by alice's key" -- they don't yet need to know "this event was signed by alice's agent-key under a grant from alice's identity-key."

### What v1 must do

**Add `session_grant_id` to the event schema.** This is the minimum hook.

On the `Event` dataclass (`_types.py:92`), add:
```python
session_grant_id: uuid.UUID | None = None
```

On the `DelegationChain` dataclass (`_types.py:65`), the existing `session_id` field serves as the session identifier. The `session_grant_id` on the event references a separate signed event (the session grant) that binds the agent's key to the human's identity for that session.

The session grant event schema:
```json
{
  "type": "session_grant",
  "grantor_id": "key:sha256:human-key-fp",
  "grantee_key_fp": "ed25519:base64:agent-pubkey",
  "session_id": "session-abc123",
  "scope": ["edit", "read", "bash:safe-subset"],
  "valid_from": "2026-05-23T14:00:00Z",
  "valid_until": "2026-05-23T22:00:00Z"
}
```

This event is signed by the **human's identity key**. The agent's tool-call events carry `session_grant_id` pointing to this event. The verifier follows the chain: event -> session grant -> human identity key.

### Session grant lifetime

- **Time-bounded:** `valid_from` / `valid_until`. Default v2 lifetime: 8 hours (one workday). Configurable.
- **Scope-bounded:** The `scope` field on the grant limits what the agent can do. The verifier checks that each tool-call event's action falls within the grant's scope.
- **Revocable mid-session:** A `session_grant_revoked` event, signed by the human's identity key, invalidates the grant. The verifier rejects events referencing a revoked grant that occurred after the revocation timestamp.

### Interaction with Q6

The auditor's attestation is signed with the human identity key directly, not an ephemeral agent key. This argues for the Q6 middle path: the auditor's key has `role: "auditor"` and signs attestations directly. The auditor never holds an agent session key. The distinction between "key that signs actions" (agent session key) and "key that signs attestations" (auditor identity key) is captured by the `role` field.

### Interaction with Claude's session-identity observation

Claude's round-2 review (section 7a) noted that session is conflated with principal. The session grant mechanism produces a natural `session_id`. The `DelegationChain.session_id` at `_types.py:67` is the right field for this. Wake events target sessions; provenance events are scoped to sessions. The session grant is the binding between the session and the identity.

### Code constraint: one signature vs. two

**Option (a): Session grant as a separate signed event.** This is the right choice. The session grant is a first-class event in the substrate log, signed by the human's key. The agent's events reference it via `session_grant_id`. Verification is a two-step lookup, not a two-signature envelope. This requires no change to `_signing.py:sign_event()` -- each event is still signed with one key. The chain is expressed through event references, not multi-signature envelopes.

---

## 6. New Q12: Temporal integrity

### Should `timestamp` be added to the signing envelope?

**Yes.** This is a one-line change to `build_signing_envelope()` at `_signing.py:10-26`:

```python
def build_signing_envelope(
    event_id: UUID,
    work_item_id: UUID,
    actor_id: str,
    transition: str | None,
    payload: dict | None,
    on_behalf_of: dict | None = None,
    timestamp: str,  # NEW: ISO 8601 UTC string
) -> bytes:
    envelope = {
        "event_id": str(event_id),
        "work_item_id": str(work_item_id),
        "actor_id": actor_id,
        "timestamp": timestamp,  # NEW
        "on_behalf_of": on_behalf_of,
        "transition": transition,
        "payload": payload,
    }
    return canonicalize(envelope)
```

**Backward compatibility:** This breaks all existing signed events. But there is no production deployment. The backward-compat retry at `_signing.py:93-98` already handles one such migration (adding `on_behalf_of`). Add a second retry path: if verification fails with `timestamp`, retry without it. This is the same pattern.

**Timing:** The timestamp must be set BEFORE signing, not after. Currently `_event_store.py:88` sets `now = datetime.now(UTC)` after the signature is computed at line 73-81. The fix is to move the timestamp generation before the `sign_event()` call and pass it through.

### Three timestamps, one ordering constraint

1. **`authenticated_at`** (in `on_behalf_of`, set by caller): When the human authenticated. This is the earliest timestamp.
2. **`timestamp`** (in event header, set by event store): When the event was created. This is the application's clock time.
3. **RFC 3161 TSA token timestamp** (from external TSA): When the batch was submitted to the TSA. This is the external authority's time.

**Ordering constraint:** `authenticated_at <= timestamp <= TSA_timestamp + tolerance`

The tolerance accounts for TSA network latency. A reasonable v1 default: 30 seconds.

**Enforcement:** The verifier tool (`agent-provenance/README.md` §6) enforces this constraint. Not substrate. Substrate's job is to produce the timestamps; the verifier's job is to check their consistency. This keeps substrate's signing path fast and simple.

### Who provides the TSA?

**Configurable per-deployment.** v1 ships with a default (FreeTSA or DigiCert public TSA -- whichever is available and free). The deployment configuration specifies the TSA URL. For SOX audits, the auditor may require a specific TSA. The verifier tool accepts a TSA configuration and validates the TSA token's signature chain.

This is a deployment concern, not a substrate concern. Substrate needs a `timestamp_batch(events, tsa_url)` function that batches events and obtains RFC 3161 tokens. The TSA URL is passed by the caller.

### Batch interval for TSA tokens

**Per-batch, not per-event.** The batch is the unit of work: a group of events submitted together. v1 default: batch on every flush (typically every 10-100 events, depending on the harness's event rate). Configurable via a `tsa_batch_size` and `tsa_batch_interval` parameter.

The TSA token covers the Merkle root of the batch. Each event in the batch includes its own timestamp (now integrity-protected by the signature), and the TSA token provides an external anchor for the batch's Merkle root. This gives per-event temporal precision (from the signed timestamp) plus external authority anchoring (from the TSA token).

### `key_id` in the signing envelope

While we're modifying `build_signing_envelope`, add `key_id` as well. Finding 3 above showed that `key_id` on the `Event` is not integrity-protected. Including it in the envelope prevents post-hoc key attribution attacks.

---

## 7. New Q13: Trust-root bootstrap

### The problem

The peer model says users exchange public keys out-of-band. But "out-of-band" is undefined. An auditor receiving a signed bundle needs the actor's public key through a channel the operator does not control. Otherwise, the operator can supply a fraudulent key and a bundle signed by the corresponding private key.

### V1 bootstrap mechanism

**Signed key-registry artifact.** At deployment setup, the operator produces a key registry:

```json
{
  "registry_id": "deployment-abc123",
  "created_at": "2026-05-23T00:00:00Z",
  "keys": [
    {
      "principal_id": "key:sha256:alice-fp",
      "public_key": "ed25519:base64:...",
      "role": "actor"
    },
    {
      "principal_id": "key:sha256:auditor-fp",
      "public_key": "ed25519:base64:...",
      "role": "auditor"
    }
  ]
}
```

This artifact is signed by the operator's key and delivered to the auditor through a channel independent of the bundle delivery channel. For the solo-dogfood case, this is a file the operator gives the auditor (or themselves) via a separate mechanism (email, USB, printed QR code).

### Solo-dogfood case

**Bootstrap is a ceremony, not a no-op.** Even when auditor and operator are the same person, the bootstrap ceremony establishes the trust root. The documentation contract: "for solo dogfood, the operator generates the key registry and the audit bundle. The verifier checks that the bundle's signatures match the registry's keys. This works for self-audit but does not provide arms-length verification."

The documentation must be explicit: "this works for dogfood but is inadequate for arms-length audit." The limitation is not in the cryptography; it's in the bootstrap. An arms-length auditor needs the public key from a source the operator doesn't control.

### Bootstrap as a signed event

**Yes.** The key registry is a signed first-class event in the log. It has a special `transition` value (`"key_registry"`) and is signed by the operator's key. This establishes: "the operator declared their public key to be X at time T." New auditors joining later can verify the key rotation chain starting from this bootstrap event.

Key rotation events extend the chain: each rotation event is signed by the old key and references the new key's fingerprint. An auditor verifies the chain from bootstrap to current key.

### DNS TXT / GitHub SSH-key directories

These are viable bootstrap mechanisms for v2. They provide an out-of-band channel the operator doesn't fully control (DNS is controlled by the domain registrar, GitHub by GitHub). For v1, the signed key-registry artifact is sufficient. The deployment documentation should describe how to achieve arms-length bootstrap: "have your auditor verify your public key through a channel you don't control (e.g., read the fingerprint from your terminal while they watch, or publish it on a domain they trust)."

---

## 8. New Q14: Identity continuity across rotation

### Opaque ID stability

**The opaque ID is stable.** `principal_id` is a human-readable identifier (e.g., `"alice"`) or a structured URI-prefix (e.g., `"key:sha256:alice-v1"`). On key rotation, the opaque ID stays the same; only the underlying key changes.

This is a departure from the "Agent positions" in the original design doc (line 414), which argued for the key fingerprint as the identifier. That position is rejected because it makes rotation painful: every event signed with the old key has a different `principal_id` than events signed with the new key. The auditor sees two different identities and cannot tell they're the same human.

### Where the binding is stored

**In the signed rotation event chain in the log.** Each rotation event:

```json
{
  "type": "key_rotation",
  "principal_id": "alice",
  "old_key_fp": "sha256:abc123",
  "new_key_fp": "sha256:def456",
  "rotated_at": "2026-06-01T00:00:00Z"
}
```

Signed by the **old key**. The verifier follows the chain: bootstrap event (alice-key-v1) -> rotation event (v1 -> v2, signed by v1) -> rotation event (v2 -> v3, signed by v2). Each link is cryptographically verified.

### What the auditor sees

When verifying an event signed by alice-key-v1 against the current key alice-key-v3, the verifier:
1. Finds the event's `key_id`
2. Looks up the key in the registry
3. If the key is `deprecated` (not `active`), follows the rotation chain forward to verify it was rotated to the current active key
4. If the chain is unbroken, the event is valid

This is the **verifier tool's responsibility**, not substrate's. Substrate provides the events and the key registry. The verifier assembles the chain.

### Does this push us back toward DIDs?

**No.** GLM-5.1 raised this as the one honest argument for DIDs. The counter-argument:

DIDs solve the same problem (identity continuity across key rotation) but add a discovery layer (DID document resolution over HTTPS) that substrate has no code for. The rotation event chain in the substrate log is simpler: it's a sequence of signed events, each verifiable with the existing `verify_event()` function. No network calls, no document resolution, no spec dependency.

The DID advantage is interoperability across systems. But the peer model (Q3 consensus) means there's only one substrate instance per deployment. Interoperability across systems is a v2+ problem. For v1, the rotation event chain is sufficient and simpler.

If v2 needs cross-system interoperability, the opaque ID can be mapped to a DID at that point. The URI-prefix encoding (`key:sha256:...`, `did:...`) already reserves the path.

---

## 9. Last bite at the apple

### Q5: Strict attribution -- concern, not objection

The consensus says every tool call is attributed to the harness operator, with `on_behalf_of` as a self-attested (not cryptographically enforced) delegation chain until BC-196 lands.

This is correct for v1. But I want to flag a subtle issue: under HMAC, the `on_behalf_of` field is covered by the signature (it's in the canonical envelope at `_signing.py:22`), which means it's integrity-protected. However, it's not authenticity-protected: anyone with the HMAC key can set any `on_behalf_of` value. The consensus correctly notes this ("cryptographically self-attested until BC-196 lands"). The implementation should document this limitation explicitly: "under HMAC, the delegation chain is tamper-evident but not authenticity-guaranteed."

No change needed. Just a documentation requirement.

### Q7: Recovery -- the `role` field doubles as the recovery hook

The consensus says recovery is v2 but the schema must hold a hook. The `role` field proposed for Q6 (`"actor" | "auditor" | "system"`) can also serve as the recovery hook by adding `"recovery"` as a valid role. A recovery key is just a key with `role: "recovery"` that is authorized to sign `key_rotation` events. This is the same one-field addition that solves Q6.

---

## 10. Anything still missed

### New Q15: Substrate-less deployment

`agent-wake/AGENTS.md` (line 288) says "HTTP-only ingest should work without substrate." If identity lives in substrate, what signs in substrate-less mode?

**Answer: The identity schema is substrate-defined but substrate-vendored.** The wire format (`principal_id`, `session_id`, `authenticated_at`, `scope`) is a simple JSON shape. A substrate-less deployment implements the same schema with a static key file and a local signing function. The schema is authoritative because substrate defines it, but the implementation is vendored.

For v1, the substrate-less path is unsigned or HMAC-signed with a local secret. The schema is the same; the verification is weaker. This is documented as "reduced assurance mode."

### New Q16: GDPR right-to-erasure collision

Kimi raised this in round 2 (section 7.3). An append-only event log containing filenames, tool arguments, or user identities conflicts with GDPR Article 17.

**Answer for v1: Redaction-by-hash.** The event payload carries a hash of sensitive fields (e.g., `tool_args_hash: sha256(canonical_json(args))`). The full cleartext lives in a separate, erasable store. The signature covers the hash, not the cleartext. When a right-to-erasure request comes in, the cleartext is deleted. The hash remains in the immutable log, preserving signature integrity. The auditor can verify that the event occurred and what its content hash was, but cannot reconstruct the cleartext.

This is already partially implemented: `agent-provenance/README.md` §7 includes `tool_args_hash` and `tool_args_redacted` in the event schema. The redaction mechanism is the design hook for GDPR compliance.

### New Q17: `alg` discriminator fail-shut rule

BC-196 requires an `alg` discriminator. The fail-shut rule: **unknown `alg` values cause verification to fail.** No warning-and-continue. The rationale: if a verifier encounters an algorithm it doesn't understand, it cannot verify the signature, which means it cannot trust the event. Treating it as "verified" would be worse than treating it as "unverified."

The exact contract: the verifier maintains a whitelist of supported algorithms. If an event's `alg` is not in the whitelist, the verifier rejects the event and logs the rejection. This is a hard rule, not a policy choice.

---

## Summary of positions

| Question | Position | V1 action |
|----------|----------|-----------|
| Q6 (reopened) | (c) `role` on `KeyEntry` | Add `role: str = "actor"` to `_keys.py:17` |
| Q10 (scope) | Operator-signed, versioned attestation events | New event type `scope_attestation` |
| Q11 (agent/human key) | Schema hook only; implement v2 | Add `session_grant_id` to `Event` |
| Q12 (temporal) | Timestamp + key_id in signing envelope | Modify `build_signing_envelope()`, move timestamp before signing |
| Q13 (bootstrap) | Signed key-registry artifact | New event type `key_registry` |
| Q14 (continuity) | Stable opaque ID, rotation event chain | No schema change; verifier responsibility |
| Q15 (substrate-less) | Vendored schema, reduced assurance | Documentation |
| Q16 (GDPR) | Redaction-by-hash, already partially designed | No change |
| Q17 (alg fail-shut) | Hard reject on unknown `alg` | Verifier contract |

### Consolidated v1 substrate changes

1. `_keys.py:17` -- Add `role: str = "actor"` to `KeyEntry`
2. `_keys.py:17` -- Add `revoked_at: str | None = None` to `KeyEntry` (for temporal revocation)
3. `_signing.py:10` -- Add `timestamp: str` and `key_id: str` to `build_signing_envelope`
4. `_signing.py:93` -- Add backward-compat retry for missing `timestamp` and `key_id`
5. `_event_store.py:70` -- Move `datetime.now(UTC)` before `sign_event()`, pass timestamp through
6. `_types.py:92` -- Add `session_grant_id: uuid.UUID | None = None` to `Event`
7. `_contract.py:582` -- Extend `validate_delegation_chain` to accept `role` on keys

All changes are additive (new optional fields with defaults) or reorderings (timestamp before signing). No field removals, no breaking changes to existing callers (the new parameters have defaults).
