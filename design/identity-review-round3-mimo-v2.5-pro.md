# Identity and multi-user — Round 3 review

**Reviewer model:** MiMo-V2.5-Pro (xiaomi-token-plan-sgp/mimo-v2.5-pro)
**Date:** 2026-05-23
**Status:** Submitted for final synthesis

---

## 1. Pre-reading attestation

| File | Most surprising finding |
|---|---|
| `identity-and-multi-user.md` | The "Agent positions" section (line 414) and the "Proposed positions" (line 292) give different answers to Q2 and the doc never flags the contradiction. Three reviewers independently caught this. |
| `round-2-response.md` (Claude) | Claude's section 7a on session identity is the strongest new observation in round 2. The DelegationChain dataclass has `session_id` but nothing uses it; the identity doc conflates principal and session. |
| `identity-review-round2-mimo.md` (MiMo r2) | The observation that `build_signing_envelope` covers `actor_id` but NOT `key_id` or `timestamp` — two fields that are integrity-relevant but outside the signature — was the sharpest code-grounded finding across all round-2 reviews. |
| `identity-and-multi-user-review-deepseek-v4-pro.md` | Deepseek's point that BC-198 Layer 1 (RFC 3161 timestamps) is implementable today against HMAC, before BC-196, is correct and underweighted by other reviewers. Temporal integrity does not require asymmetric signing. |
| `review-kimi-k2.6-turbo.md` | Kimi's agent-key vs. human-key topology (section 7.1) is the single most important new question from round 2. It reframes the entire delegation discussion from "schema fields" to "cryptographic topology." |
| `review-qwen3.6-27b-identity-multi-user.md` | Qwen's observation that `_keys.py` has no per-actor key resolution — `KeySet` is a single shared keyring with one active key — means the multi-user design requires a deeper key-model change than most reviewers assume. |
| `reviews/identity-and-multi-user-gemini-3.5-flash.md` | Gemini's harness heartbeat proposal (section 7.1) is the only reviewer who engaged with the completeness problem (missing events) rather than just the integrity problem (tampered events). This directly serves agent-provenance §2. |
| `reviews/identity-and-multi-user-glm-5.1.md` | GLM's point that `KeyEntry.secret: bytes` cannot distinguish HMAC keys from Ed25519 private keys at the data-model level is a real code constraint that BC-196 must address. |
| `_signing.py` | `build_signing_envelope` does not include `timestamp`. This is a one-line fix with backward-compat implications that no reviewer proposed a concrete migration path for. |
| `_keys.py` | `verify_key_status()` at line 166-175 rejects revoked keys unconditionally — no `revoked_at` timestamp, no "events before revocation are valid" logic. This was flagged by 6/7 reviewers but no one proposed the exact schema change. |
| `_types.py` | `DelegationChain` dataclass (line 64-88) is dead code — nothing imports it. But its existence means the delegation schema is already decided: `principal_id`, `session_id`, `authenticated_at`, `scope`. |
| `_event_store.py` | **New finding (see §2):** In `PostgresEventStore.append()` (line 250-286), the `INSERT` statement does not include `timestamp` — the database sets it via a column default. The Python-side `datetime.now(UTC)` at line 88 is only used for the `InMemoryEventStore`. This means the clock source differs between backends. |
| `agent-provenance/README.md` | §2's requirement that the scope statement be a signed first-class event is the most load-bearing sentence in the document and was missed by the original design doc's nine questions. |
| `breadcrumbs/196` | Status is `accepted`, not `implemented`. The pluggable signature interface is the structural prerequisite for everything and has no code. |
| `breadcrumbs/197` | Status is `implemented` but the acceptance criteria still have unchecked boxes for per-principal signing keys and external auth-provider integration. The implementation was the typed `on_behalf_of` sub-object; cryptographic enforcement is explicitly deferred to BC-196. |
| `breadcrumbs/198` | Layer 1 (RFC 3161 timestamps) is "implementable today against the existing HMAC primitive" (line 189-190). This is the strongest argument for addressing temporal integrity before identity. |

---

## 2. Substrate code findings beyond what prior reviewers caught

### 2.1 Clock-source divergence between InMemoryEventStore and PostgresEventStore

This is the one genuinely new code finding. In `_event_store.py:88`, `datetime.now(UTC)` sets the timestamp for the `InMemoryEventStore` path. But in `PostgresEventStore.append()` at line 258-286, the `INSERT` statement does not include a `timestamp` column — it uses `RETURNING timestamp`, which means the database column default (likely `now()` or `CURRENT_TIMESTAMP`) provides the timestamp. The Python-side `now` at line 88 is never used for the Postgres path.

This means:
- The in-memory backend's clock is the Python process clock.
- The Postgres backend's clock is the database server clock.
- If these clocks diverge (different machines, VM clock drift), the same code produces events with different timestamps depending on the backend.

For Q12 (temporal integrity), this reinforces the case for adding `timestamp` to the signing envelope: it makes the timestamp source explicit rather than implicit and backend-dependent.

### 2.2 The `_load()` method silently accepts unknown JSON keys

`_keys.py:61` checks that `"keys"` is present in the top-level JSON, but the per-key loop at line 70-93 only reads `key_id`, `status`, and `secret`. Any additional fields in the JSON (like a future `role`, `recovery_key_fp`, `public_key`, or `attestation_type`) are silently ignored. This is actually good news for schema evolution — we can add fields to `KeyEntry` and the key file format without breaking existing loaders, as long as the new fields have defaults.

### 2.3 `active_key()` returns the first active key found

`_keys.py:94-95` sets `new_active` to the first key with `status == "active"`. If the key file has multiple active keys (which would happen during a rotation window), only the first one in JSON array order is used for signing. This is a latent bug for rotation: if someone adds a new active key before deprecating the old one, the old key keeps signing because it appears first. The `KeySet` should either enforce at-most-one-active or pick the most recently added active key.

---

## 3. Q6 (reopened): Auditors

**Position: (c) middle path — same identity primitive, `role` field on `KeyEntry` for signing-scope enforcement.**

### Argument from code

The Q6 question is whether auditors need a separate identity primitive or can reuse the actor identity. The answer from the code is clear: `_signing.py:sign_event()` takes `actor_id: str` and `on_behalf_of: dict | None`. It does not know or care whether the signer is an actor or an auditor. The signing envelope is the same shape. The `KeyEntry` dataclass at `_keys.py:17` is the same shape. Creating a separate "auditor identity primitive" would mean a second signing path, a second key registry, and a second verification flow — all for a difference that is *semantic* (what events this key is allowed to sign), not *structural* (how signatures work).

Option (a) — original consensus, auditors are just another identity — is correct about the primitive but wrong about the enforcement. If an auditor's key can sign a `tool_call` event, a compromised auditor key can forge actions. The auditor's key should be restricted to signing attestation events only. The original consensus says "the auditor's key is not an 'actor identity'" but provides no mechanism to enforce this.

Option (b) — Kimi/GLM dissent, separate auditor primitive — is correct about the threat model but wrong about the implementation cost. Separate key registries, separate signing envelopes, separate verification paths — this doubles the surface for a distinction that affects only the verifier's event-type check.

Option (c) — middle path — gets both:

1. **Same signing path.** Auditor attestations go through `sign_event()` with the same envelope shape as actor events. No second signing module.
2. **Same key registry.** Auditor keys live in the same `KeySet` JSON file. No second registry.
3. **`role` field on `KeyEntry`.** Added to the dataclass at `_keys.py:17` as `role: str = "actor"`. The key file JSON gains an optional `"role"` field. Values: `"actor"`, `"auditor"`, `"recovery"`. The `_load()` method at `_keys.py:70` reads it with a default.
4. **Verifier enforcement.** The verifier tool (agent-provenance's offline verifier) checks: if the event is a `tool_call` or state transition, reject if the signing key's `role` is `"auditor"`. If the event is an attestation, accept if the signing key's `role` is `"auditor"`. This is a policy check, not a cryptographic one — but it's the right layer. The cryptographic signature is valid either way; the policy check prevents privilege escalation.

The `role` field also serves Q7 (recovery): a recovery key has `role: "recovery"` and is only valid for signing `key_rotation` events. One field, three uses.

### Why the threat-model argument for (b) is overstated

Kimi argues that "an auditor's key compromise corrupts the verification layer itself." This is true, but it's an argument for stronger key management for auditor keys (hardware-backed, shorter rotation cycle), not for a separate primitive. The `role` field plus operational policy ("auditor keys must be hardware-backed") gives the same defense without the structural duplication. A compromised auditor key that can only sign attestation events is less dangerous than a compromised actor key that can sign tool-call events — the attestation events are verifiable claims, not actions.

---

## 4. New Q10: Scope attestation

### Proposal

**Who signs:** The operator, as a bootstrap event signed at substrate init (or at first configuration of harnesses). This is not a periodic attestation — it's a declaration.

**Versioning:** Yes, scope attestation must be versioned. Each version is a separate signed event in the log. The event schema:

```
event type: scope_declaration
harnesses:
  - name: "claude-code"
    version: "2.1.80"
    config_digest: "sha256:..."
  - name: "opencode"
    version: "0.x.y"
    config_digest: "sha256:..."
effective_from: "2026-05-23T10:00:00Z"
supersedes_event_id: "<event_id of previous scope declaration>"  # null for first
```

**Scope evolution:** Each change (add/remove a harness) requires a new scope attestation event. This is cheap — it's an append-only log, and the new event references the previous one via `supersedes_event_id`. The auditor's verifier builds a point-in-time scope by walking the chain.

**Harness heartbeats:** Valuable but v2. For v1, the scope attestation event plus the `config_digest` on each tool-call event (already in the agent-provenance event schema sketch at `agent-provenance/README.md:188`) gives the auditor enough to verify "this event was produced by a configured harness." Heartbeats add the *temporal* dimension ("the harness was still alive between events"), which is a gap-detection primitive. It's important but not required for the first credible audit.

**Implementation path:** The scope attestation is a substrate event type, signed by the operator's key, using the existing `sign_event()` path. It does not require any change to `_signing.py` or `_keys.py`. It requires a new event schema in agent-provenance's adapter layer. The verifier tool needs a "scope check" pass that walks the scope-declaration chain and validates each event's `config_digest` against the effective scope.

---

## 5. New Q11: Agent-key vs. human-key topology

This is the highest-priority new question and the one I spent the most time on.

### Position: v1 implements single-key signing; v1 schema design permits two-key verification in v2.

### Why v1 cannot implement two-key verification

`_signing.py:sign_event()` at line 43-57 returns a single `(signature, canonical_hash, envelope)` tuple. `verify_event()` at line 72-99 verifies one signature against one key. Two-key verification (agent key for the event, human key for the session grant) requires either:

(a) **Two signatures on every event.** This changes the return type of `sign_event`, the `Event` schema (which stores one `signature: bytes` at `_types.py:106`), the `verify_event` logic, and the database schema. This is a breaking change to the signing surface.

(b) **Session grant as a separate event, referenced by action events.** The action event carries a `session_grant_event_id` reference. The verifier checks: (1) the action event's signature against the agent key, (2) the referenced session grant event's signature against the human key, (3) the session grant's scope and expiry. This is structurally cleaner — it uses the existing single-signature path and adds a cross-reference check — but it requires the verifier to follow links, which is more complex.

Option (b) is the right path for v2. It composes with the existing substrate infrastructure:
- The session grant is a signed event in the log, like any other event.
- It carries `on_behalf_of` with `principal_id` = the human, `scope`, and a new field `grantee_pubkey` = the agent's ephemeral public key.
- It has an `expires_at` timestamp.
- Action events carry `session_grant_event_id` in their `on_behalf_of` dict.
- The verifier checks the grant's validity (not expired, granted by the claimed human, scope covers the action) in addition to the action event's signature.

### What v1 must do to permit this

The v1 schema must not foreclose option (b). The specific constraints:

1. **`on_behalf_of` dict must accept additional fields.** `_contract.py:validate_delegation_chain()` at line 582-613 validates `principal_id`, `scope`, and `authenticated_at` but does not reject unknown fields. This is already correct — the dict is open. Adding `session_grant_event_id` later requires no schema change.

2. **`KeyEntry` must eventually hold a public key field.** For two-key verification, the agent's ephemeral key needs a public key in the key registry. Currently `_keys.py:KeyEntry` has `secret: bytes` (line 19) with no public key. When BC-196 lands and `KeyEntry` gains `public_key: bytes | None` and `alg: str`, the session grant mechanism can reference the grantee's public key by fingerprint. The v1 `KeyEntry` schema should add a `public_key` field (optional, default `None`) alongside the `role` field from Q6.

3. **`DelegationChain` dataclass should gain `session_grant_event_id`.** The dataclass at `_types.py:64-88` is currently dead code, but when it becomes the canonical typed representation of `on_behalf_of`, it should carry this field. Add it as optional now.

### Session grant lifetime

Time-bounded (the `expires_at` field on the grant event). Scope-bounded (the `scope` field restricts what actions the agent key can sign). Revocable mid-session (the human signs a `session_revocation` event that references the grant; the verifier rejects any action events signed after the revocation's timestamp). This composes with the existing key lifecycle — the agent's ephemeral key can be revoked independently of the human's identity key.

### Interaction with Q6

An auditor's attestation would be signed with the human identity key directly, not an ephemeral agent key. This is correct: the auditor is a human principal, not an agent session. The `role: "auditor"` field on `KeyEntry` (from Q6) applies to the human key, not the agent session key. The auditor does not need a session grant.

### Interaction with Claude's session-identity observation

Claude's round-2 observation (section 7a) that session is conflated with principal is resolved by the session grant mechanism: the grant event produces a natural `session_id` (the grant event's UUID). Wake events target a session by referencing the grant event ID. Provenance events are scoped to a session by carrying the same reference. The session lifecycle (created = grant signed, active = agent signing events, terminated = grant expired or revoked) is a first-class concept in the log.

---

## 6. New Q12: Temporal integrity

### Concrete proposal

**Add `timestamp` to the signing envelope.** One-line change to `build_signing_envelope()` at `_signing.py:10-26`:

```python
def build_signing_envelope(
    event_id: UUID,
    work_item_id: UUID,
    actor_id: str,
    transition: str | None,
    payload: dict | None,
    on_behalf_of: dict | None = None,
    timestamp: str | None = None,  # NEW — ISO 8601 string
) -> bytes:
    envelope = {
        "event_id": str(event_id),
        "work_item_id": str(work_item_id),
        "actor_id": actor_id,
        "on_behalf_of": on_behalf_of,
        "transition": transition,
        "payload": payload,
    }
    if timestamp is not None:  # backward-compat: old envelopes omit it
        envelope["timestamp"] = timestamp
    return canonicalize(envelope)
```

**Backward compatibility:** The `verify_event` function at `_signing.py:72-99` already has a backward-compat retry path (line 92-98) for events without `on_behalf_of`. Add a second retry for events without `timestamp`. Since there are no production deployments, the backward-compat window is small — only events from development/testing. A migration tool can re-sign old events if needed.

**Acceptable now because:** There are no production deployments. The longer we wait, the more events exist without timestamp integrity. This is the cheapest time to make this change.

### The three timestamps

1. `authenticated_at` — set by the caller when the human authenticates. Lives in `on_behalf_of`. Advisory — self-attested by the caller until BC-196 lands.
2. `timestamp` — set by the event store. Lives in the event header and (after this change) in the signed envelope. Integrity-protected by the signature.
3. RFC 3161 token timestamp — set by the external TSA. Lives in a `timestamp_anchor` event. Cryptographically bound to the event batch hash.

**Ordering constraint:** `authenticated_at ≤ event.timestamp ≤ TSA timestamp + tolerance`. This should be enforced by the verifier tool, not by substrate. Substrate validates structure; the verifier validates semantic consistency. The tolerance window is deployment-configurable (default: 1 hour) because clock skew between the human's device, the substrate server, and the TSA is real.

### Who provides the TSA

Configurable per-deployment. v1 ships with a default (FreeTSA or a similar public TSA) and documents the swap path. For SOX, the auditor may require a TSA they trust — the deployment config accepts a TSA URL and certificate chain. The `timestamp_anchor` event schema stores the TSA's certificate fingerprint so the verifier can check which TSA was used.

### Batch interval

v1 default: 5 minutes. Configurable via substrate config. The trade-off is granularity vs. cost:
- Per-event: too expensive for public TSAs, fine for commercial.
- Hourly: too coarse for some audits (a tool call at 14:01 and another at 14:59 would be in the same batch).
- 5 minutes: reasonable default. Short enough for most audit requirements, cheap enough for public TSAs.

Per-event timestamping should be supported as an option for deployments that need it and are willing to pay the TSA costs.

---

## 7. New Q13: Trust-root bootstrap

### Concrete proposal

**v1 mechanism: manual key-exchange ceremony + signed bootstrap event.**

The process:
1. Operator generates a keypair (CLI command, not yet built — tracked as part of Q7 key management).
2. Operator and auditor meet (physically or via a verified channel).
3. Operator presents their public key fingerprint. Auditor records it.
4. Operator signs a `key_declaration` event in the substrate log: "my public key fingerprint is X, my opaque ID is Y." This event is signed by the key it declares.
5. Auditor verifies the event signature against the fingerprint they received out-of-band.

The `key_declaration` event is the bootstrap anchor. It's a signed first-class event in the log that says "at time T, this key claimed this identity." Without it, a new auditor joining later cannot verify key rotation history — they'd see rotation events but have no root of trust to start the chain.

**For the solo-dogfood case:** Bootstrap is a no-op in practice — the operator generates a key, signs events, and verifies themself. But the `key_declaration` event still gets created. This makes the dogfood log structurally identical to a production log. The documentation contract: "this works for dogfood because the auditor and operator are the same person. For arms-length audit, the auditor must verify the key fingerprint through a channel the operator does not control."

**For enterprise deployments:** The bootstrap mechanism can be backed by DNS TXT records (operator publishes their key fingerprint at `_agent-provenance.example.com`), GitHub SSH-key directories (operator's key fingerprint matches their GitHub SSH key), or enterprise SSO certificates (operator's key is signed by the corporate CA). These are deployment-layer concerns, not substrate concerns. Substrate stores the `key_declaration` event; the deployment decides what evidence is sufficient for the auditor to trust it.

**Is the bootstrap event itself signed?** Yes. The `key_declaration` event is signed by the key it declares. This sounds circular — "I declare that my key is X, signed by X" — but it's not: the signature proves the declarer holds the private key corresponding to the declared fingerprint. The out-of-band channel (meeting, DNS, GitHub) proves the declarer is who they claim to be. The two together establish the binding.

---

## 8. New Q14: Identity continuity across key rotation

### Position: Stable opaque ID, binding in rotation event chain, verification is the verifier's responsibility.

**The opaque ID is a stable name (`"alice"`), not a key fingerprint.** If it were a key fingerprint, it would change on rotation, and the auditor would see two disconnected identities. The consensus on Q2 (opaque ID + attestation-type) already implies this — the `id` is opaque and deployment-chosen, not derived from the key.

**The binding is stored in signed rotation events in the log.** When alice rotates from key-v1 to key-v2, she signs a `key_rotation` event: `{old_key_fp: "sha256:abc", new_key_fp: "sha256:def", principal_id: "alice"}`, signed by key-v1. This event is the binding. The chain of rotation events from the current key back to the original `key_declaration` event constitutes the identity history.

**The verifier follows the rotation chain.** When verifying a historical event signed by key-v1, the verifier:
1. Extracts `key_id` from the event.
2. Looks up the key in the key registry (or in the log's rotation events).
3. Finds the rotation event that supersedes key-v1.
4. Follows the chain to the current key.
5. Confirms the chain is unbroken (every rotation event is signed by its predecessor).

This is the verifier tool's responsibility, not substrate's. Substrate stores the events; the verifier interprets them.

**Does this push us back toward DIDs?** No. The DID ontology provides exactly this — a stable identifier with a key rotation history — but it also provides a discovery layer (DID resolution) that substrate has no code for and no need for. We're building a simpler version of the same mechanism: rotation events in an append-only log, verified by walking the chain. The DID spec's key-rotation model is battle-tested, but the DID resolution model is overkill for a system where the log itself is the registry. We can borrow the rotation semantics without importing the full DID stack.

The one honest argument for DIDs that GLM raised (and that no prior reviewer engaged with) is that DIDs have a standardized rotation history mechanism. But we already have the mechanism: signed rotation events in the log. The log *is* the DID document, in a sense — it's just not formatted as JSON-LD.

---

## 9. Last bite at the apple

I have one sharp objection to a locked position.

### The Q7 locked position says "recovery is a v2 feature but the v1 schema must hold a hook for it."

The locked position says the v1 schema hook is either `role: "primary" | "recovery"` on `KeyEntry` or `recovery_pubkey` as a separate optional field. I agree with the hook. But the locked position's framing — "recovery is a v2 feature" — undersells the urgency.

Here's the problem: if v1 ships without recovery and an operator loses their key during dogfooding, the entire audit log becomes permanently unverifiable. For a hobbyist project, this is annoying. For a workplace deployment where the compliance team has signed off on the audit trail, this is a career-limiting event. The compliance team cannot explain to an external auditor why the provenance chain is broken.

The v1 *implementation* of recovery can be minimal: the operator runs a CLI command that signs a `key_rotation` event with the recovery key. The recovery key is an Ed25519 keypair stored on a USB drive. The CLI reads the recovery key from the USB, signs the rotation event, writes it to the log. This uses the existing `sign_event()` path with no changes.

The v1 *schema* change is one field on `KeyEntry` (`role: str = "primary"`) plus the recovery key in the key file. The v1 *implementation* change is a CLI command. The total code is perhaps 50 lines.

I'm not arguing the locked position is wrong — I'm arguing the bar for "v2 feature" is too high. A minimal recovery implementation belongs in v1 because the consequence of not having it (permanent identity loss) undermines the driving use case.

---

## 10. Anything still missed

### 10.1 The `_contract.py` validation doesn't reject unknown fields in `on_behalf_of`

`validate_delegation_chain()` at `_contract.py:582-613` validates `principal_id`, `scope`, and `authenticated_at` but does not reject unknown keys in the dict. This is actually *correct* for forward compatibility — new fields like `session_grant_event_id` can be added without breaking old validators. But it should be documented as intentional, not accidental. A future validator that adds `required` field checks could accidentally reject events with new fields if the validation logic is rewritten without this in mind.

### 10.2 No event schema version field

The `Event` dataclass at `_types.py:91-108` has no schema version field. When the signing envelope changes (add `timestamp`, add `alg`, add identity fields), the verifier needs to know which envelope shape to expect. Substrate's current approach is implicit backward-compat retry paths in `verify_event()` (line 92-98). This won't scale. A `schema_version: int` field on the event (or on the signing envelope) would let the verifier dispatch to the right verification logic without trial-and-error. This is a v1 requirement if the `timestamp` change lands, because the backward-compat path for "events without timestamp" needs to coexist with "events with timestamp" and "events with timestamp and `alg`."

### 10.3 Substrate-less identity

If identity lives in substrate but agent-wake can run without substrate (per `AGENTS.md`), what signs events? The proposal: agent-wake gets a lightweight signing module that reads a key file (same JSON format as `_keys.py` expects) and signs events before sending them to the HTTP ingest endpoint. The key file is the same format; the signing logic is the same `build_signing_envelope` + `compute_hmac` (or later, Ed25519). This means the signing module should be extracted from substrate into a shared library, or agent-wake should vendor the relevant functions. This is an implementation detail, not a design question, but it should be tracked.

### 10.4 GDPR and append-only logs

Kimi raised this and it deserves a concrete answer. The proposal: field-level redaction events. A `redaction` event in the log references a prior event and a field path, and states "this field contained personal data that has been erased per GDPR Article 17." The hash of the redacted field remains in the original event's signature (the signature is immutable), but the cleartext is gone. The verifier sees the redaction event and knows the field is no longer available. This does not break the signature chain — it adds a post-hoc annotation that the cleartext has been removed.

The catch: the hash of the redacted field is itself pseudonymized personal data under GDPR. If the hash can be reversed (dictionary attack on likely values), it's not truly anonymized. The honest answer: this system is not designed for GDPR erasure. It's designed for audit logs where the data subject has consented to logging as a condition of using the system. The GDPR collision should be documented, not engineered around.

---

## Summary table

| Question | Position |
|---|---|
| Q6 (auditors) | (c) Middle path: same identity primitive, `role` field on `KeyEntry` for signing-scope enforcement |
| Q10 (scope attestation) | Operator-signed bootstrap event, versioned chain, harness heartbeats are v2 |
| Q11 (agent-key vs. human-key) | v1 schema permits session grants; v2 implements two-key verification via separate grant events |
| Q12 (temporal integrity) | Add `timestamp` to signing envelope now (no production deployments yet); 3-timestamp ordering enforced by verifier; configurable TSA, 5-min default batch |
| Q13 (trust-root bootstrap) | Manual key-exchange + signed `key_declaration` event; enterprise deployments can back with DNS/GitHub/SSO |
| Q14 (identity continuity) | Stable opaque ID, binding in rotation event chain, verifier follows the chain. Not DIDs. |
| Last bite | Recovery implementation belongs in v1, not v2 — 50 lines of code prevents permanent identity loss |
