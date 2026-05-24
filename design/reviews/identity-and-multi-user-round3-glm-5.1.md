# Round 3 Final Design Review: Identity and Multi-User

**Reviewer:** glm-5.1 (zai-coding-plan/glm-5.1)
**Date:** 2026-05-23
**Round:** 3 (final before implementation planning)

---

## 1. Pre-reading attestation

| File | Most surprising finding |
|------|------------------------|
| `design/identity-and-multi-user.md` | The "Proposed positions" (line 290) and "Agent positions" (line 414) propose *different wire formats* — `{id, attestation_type}` vs raw key fingerprint — without flagging the contradiction. Two rounds of review have engaged this without the doc reconciling it. |
| `design/round-2-response.md` (Claude) | Strongest finding: session identity (7a) and the harness trust boundary difference between Claude Code (subprocess) and opencode (in-process). No other reviewer caught the in-process signing key exposure. |
| `design/identity-review-round2-mimo.md` | Flagged that the signing envelope covers `actor_id` but not `key_id` — the key that signed is outside the signed payload. This means `key_id` is forgeable by anyone who can write to the event store. |
| `design/identity-and-multi-user-review-deepseek-v4-pro.md` | Identified the key distribution bootstrap problem (7.2): if the auditor gets the public key *from the operator*, the operator can supply a fraudulent key. No prior reviewer before round 2 caught this. |
| `design/review-kimi-k2.6-turbo.md` | The strongest single observation across both rounds: agent-key vs. human-key topology (7.1). The current design has agent and human sharing one key, making `on_behalf_of` structurally present but cryptographically meaningless. |
| `design/review-qwen3.6-27b-identity-multi-user.md` | Pointed out that `actor_id` (`_types.py:96`), `principal_id` (`_types.py:66`), and the design doc's `trigger_identity` are three identity fields with no mapping between them. |
| `design/reviews/identity-and-multi-user-gemini-3.5-flash.md` | Proposed harness heartbeats (7.1) as the completeness mechanism. Practical, implementable, and directly addresses agent-provenance's scope-completeness requirement. |
| `design/reviews/identity-and-multi-user-glm-5.1.md` (prior round) | Raised identity migration across key rotation — the one honest argument for DIDs that nobody else engaged. |
| `_signing.py` | `build_signing_envelope()` (line 10-26) covers `actor_id`, `on_behalf_of`, `transition`, `payload` — but **not** `key_id` and **not** `timestamp`. Two load-bearing fields sit outside the signature. |
| `_keys.py` | `KeyEntry` (line 17-20) is three fields: `key_id: str`, `secret: bytes`, `status: str`. No `public_key`, no `alg`, no `fingerprint`, no `revoked_at`, no `role`. The entire key model is HMAC-shaped. |
| `_types.py` | `DelegationChain` dataclass (line 64-88) is typed and frozen with `to_dict`/`from_dict` — but nothing instantiates it. `ActorKind` enum (line 13-16) has `AGENT`, `HUMAN`, `SYSTEM` — a role taxonomy the design doc doesn't mention. |
| `_event_store.py` | Line 88: `now = datetime.now(UTC)` — timestamp set *after* signing (lines 73-81). The timestamp is not in the signed envelope. For Postgres backend, line 323 shows the DB overwrites the timestamp with `RETURNING timestamp`, meaning the event store uses the DB server's clock, not the client's. |
| `_contract.py` | `validate_delegation_chain()` (line 582-613) validates `principal_id` as non-empty string, `scope` as list-of-strings, `authenticated_at` as string. No format constraint on `principal_id`. |
| BC-196 | Status: `accepted`. Line 106: acceptance criteria require `alg` discriminator. Line 125: HMAC stays as zero-config default. Pluggable signing is the prerequisite for everything else. |
| BC-197 | Status: `implemented`. But line 168: "cryptographic enforcement is BC-196 + future work." The `on_behalf_of` field is schema-present and HMAC-protected but self-attested — anyone with the HMAC key can forge any `principal_id`. |
| BC-198 | Status: `accepted`. Line 189: "Layer 1 is implementable today against the existing HMAC primitive." This means temporal integrity does not wait for BC-196. Layer 1 is a pure HTTP call to a TSA. |
| `agent-provenance/README.md` | Line 46-50: scope statement must be a signed first-class event. Line 99-103: "no cryptographic primitive defends against the operator chose not to record" — honest. Line 229: "Real IdP integration (OIDC, SAML) is a v2 question." |

### What stood out across reviewers

Three themes dominated round 2:

1. **Temporal integrity** (5/7 reviewers): the timestamp gap in `_signing.py` is the single most cited code-level defect.
2. **Agent vs. human key topology** (Kimi's 7.1): the deepest structural observation, implying that `_signing.py:sign_event()` taking a single `key: bytes` is a fundamental architectural limitation.
3. **Scope attestation** (5/7 reviewers): `agent-provenance/README.md` §2 requires it, the design doc ignores it, and it surfaces immediately when you try to answer "how does the auditor know they saw everything?"

---

## 2. Substrate code findings beyond prior reviewers

### The Postgres timestamp overwrite

Prior reviewers correctly noted that `datetime.now(UTC)` at `_event_store.py:88` runs after signing. But there is a subtler gap: the *Postgres* backend (line 250-330) does `INSERT ... RETURNING timestamp` — the database column has a `DEFAULT now()`, and the returned row's timestamp *overwrites* the client-side `now`. This means:

- For `InMemoryEventStore`, the timestamp is the client's `datetime.now(UTC)`.
- For `PostgresEventStore`, the timestamp is the Postgres server's `now()`.

An operator who controls the Postgres server clock (trivial on a self-hosted deployment) can backdate events even if the client timestamp were signed. This reinforces the need for RFC 3161 anchoring — the only clock the operator cannot control is the TSA's.

### `ActorRole` already exists

`_types.py:651-669` defines `ActorRole(actor_id: str, role: str, created_at: datetime)` with `to_dict`/`from_dict`. This is a deployment-level role assignment that substrate already stores. It maps directly to the `role` field several reviewers proposed for `KeyEntry` — except `ActorRole` binds role to `actor_id`, not to `key_id`. The design could reuse `ActorRole` for the auditor/actor distinction without adding anything to `KeyEntry`.

### `ActorKind` covers the session-identity gap

`_types.py:13-16` has `AGENT`, `HUMAN`, `SYSTEM`. This enum already distinguishes "who performed this action" at the type level. The design doc's `trigger_identity` vs `actor_identity` could be expressed as two events with different `actor_kind` values linked by a typed link (substrate already has `LinkTypeDef`), rather than as two fields on one event. This avoids changing the signing envelope.

---

## 3. Q6 (reopened): Auditors

**Position: (c) middle path — `role`/`capability` on `KeyEntry`, enforced by `ActorRole`.**

The Kimi/GLM dissent argues that auditor key compromise corrupts the verification layer, which is worse than actor key compromise. The original consensus says auditors are just identities. I pick a middle path, but I ground it differently than prior reviewers.

### Why not (a) original consensus

Under the consensus, an auditor is "just another identity." But the code already has `ActorKind` at `_types.py:13-16` distinguishing `AGENT`, `HUMAN`, `SYSTEM`. If an auditor signs an attestation event, it goes through `sign_event()` at `_signing.py:43` with `actor_id` set to the auditor's identifier and `actor_kind` set to... what? `HUMAN`? That's the same as a regular operator. The code has no way to express "this event is an auditor attestation" vs "this event is a tool call" at the type level.

The consensus position forces auditor events into the same stream as actor events with no structural distinction. An auditor verifying the log cannot programmatically separate "events I should check" from "events I signed." This is a practical problem in a log with thousands of events.

### Why not (b) Kimi/GLM full dissent

The dissent proposes auditor access as "a separate token / public-key allowlist" with attestations stored as "bundle co-signatures, not events in the live log." This creates two systems:

1. The substrate event log (for actor events).
2. A separate attestation store (for auditor attestations).

The auditor then needs two verification paths. Substrate's replay machinery (`_replay.py`) covers only the event log. Auditor attestations would need a parallel replay infrastructure. This is over-engineering for a single-person OSS project.

### The middle path: role on KeyEntry, constrained by ActorRole

```
Changes to _keys.py:
  KeyEntry gains: role: str = "actor"   # "actor" | "auditor"

Changes to _contract.py:
  validate_delegation_chain() or a new validate_key_role():
    - Auditor keys can only sign events where transition ∈ {"auditor_attestation", "scope_attestation"}
    - Actor keys cannot sign attestation events

Existing ActorRole at _types.py:651:
  - Reused for deployment-level role assignment
  - role: "auditor" → read-only in the event log, write-only for attestations
```

This is a 3-line change to `KeyEntry` and a ~20-line validation function. It uses `ActorRole` that already exists. It gives auditors the same identity primitive (same `principal_id`, same signing envelope) but constrains what they can sign. Auditor key compromise can forge attestations (bad) but cannot forge tool-call events (worse for the actor model).

For v1, the enforcement is advisory — the validation runs at event append time, not at the cryptographic level. A malicious operator could bypass it by editing the validation code. But the constraint is *auditable*: an auditor verifying the log can check that all `auditor_attestation` events were signed by keys with `role: "auditor"` and no `tool_call` events were. This is sufficient for the workplace-audit use case.

---

## 4. Q10: Scope attestation

### Concrete proposal

**Who signs the scope statement?** The operator's identity key at substrate initialization. The scope attestation is the first event in the log (event_seq 0 for a special "scope" work item), signed by the operator's active key. It is not a bootstrap event — it is a configuration event.

**Schema:**

```
scope_attestation event:
  transition: "scope_attestation"
  actor_kind: "system"
  payload:
    harnesses:
      - name: "claude-code"
        config_digest: "sha256:..."
      - name: "opencode"
        config_digest: "sha256:..."
    scope_version: 1
    valid_from: "2026-05-23T00:00:00Z"
    valid_until: null              # null = currently active
    supersedes: null               # null = initial scope
  on_behalf_of:
    principal_id: "key:sha256:..."  # operator's key fingerprint
```

**Is scope versioned?** Yes. Each scope change is a new event with `scope_version` incremented and `supersedes` pointing to the previous scope attestation's `event_id`. This is structurally identical to key rotation — a linked list of scope statements.

**Scope evolution mechanism:** A new scope attestation per change, not a scope-rotation event. The reason: scope changes have different semantics than key rotation. Adding a harness is a *widening* of scope; removing a harness is a *narrowing*. These need different audit narratives. A single event type with versioning handles both.

**How the auditor uses it:**

1. Replay the event log.
2. For each tool-call event at timestamp T, find the scope attestation whose `valid_from <= T < valid_until`.
3. Verify the event was emitted by a harness listed in that scope version.
4. Any event from an unlisted harness is an out-of-scope anomaly.

**Are harness heartbeats in scope for v1?** Yes, but minimal. Each configured harness emits a heartbeat event every N minutes (configurable, default 5 minutes). The heartbeat is:

```
heartbeat event:
  transition: "heartbeat"
  actor_kind: "agent"
  payload:
    harness: "claude-code"
    session_id: "..."
    uptime_seconds: 3600
```

If an auditor's verifier sees a gap in heartbeats longer than 2x the interval, it raises a "gap detected" warning. This is not cryptographic proof of missing events — it is a heuristic. But it is the best practical answer to the completeness problem without endpoint-level instrumentation.

**Why this works for v1:** The scope attestation reuses substrate's existing event machinery. It goes through `sign_event()` and is covered by the HMAC (later Ed25519) signature. No new substrate primitives needed. The heartbeat is a regular event with a standard transition name. The auditor's verifier tool does the scope-matching logic.

---

## 5. Q11: Agent-key vs. human-key topology

This is the highest-priority new question. My position is grounded in `_signing.py:43` — `sign_event()` takes one `key: bytes` and returns one signature.

### Should v1 support this topology?

**No. V1 should design the schema to permit it later.** Here is why:

The two-key topology requires `sign_event()` to either:
- (a) Accept two keys and produce two signatures (breaking the function signature), or
- (b) Have the session grant be a separate signed event referenced by every action event.

Option (a) changes the return type of `sign_event()` from `tuple[bytes, bytes, bytes]` to something that carries two signatures. This ripples through every caller: `_event_store.py:73`, `_transition.py`, `_in_memory_transition.py`, `_ops.py`, `_events_api.py`, and `__init__.py`. The `Event` dataclass at `_types.py:92` would need a second signature field. The Postgres schema would need a new column. This is the most invasive possible change and it blocks on BC-196 (need asymmetric keys first).

Option (b) is the right architecture but it is v2. The session grant is a signed event in the log (e.g., `transition: "session_grant"`) that carries the agent's ephemeral public key, the human's key fingerprint, the session scope, and an expiry. Every action event references the session grant's `event_id` via substrate's existing `on_behalf_of.session_id` field (already present at `_types.py:67`). The verifier follows the reference: check the action event's signature against the agent key, check the session grant's signature against the human key, check the grant hasn't expired.

**V1 action:** Ensure the `on_behalf_of` schema can carry the session grant reference. The existing `session_id` field at `_types.py:67` (currently unused) is exactly this hook. No schema change needed. Document that in v1, `session_id` is opaque and not cryptographically verified; in v2, it references a session grant event.

### What's the lifetime of a session grant?

**Time-bounded and scope-bounded.** The session grant carries:
- `expires_at`: absolute UTC timestamp. Default: 8 hours (one work session).
- `scope`: list of permitted actions. Maps to the existing `on_behalf_of.scope` field at `_contract.py:596`.

**Revocable mid-session?** Yes, by the human's identity key. A `session_revocation` event (signed by the human key) referencing the session grant's `event_id` causes the verifier to reject all subsequent action events referencing that session. This is structurally identical to key revocation — a signed event that invalidates a previous signed event.

### How does this interact with Q6 (auditors)?

An auditor's attestation is signed by the auditor's identity key directly (long-lived, not ephemeral). The auditor does not operate through a session grant. This is a natural consequence of the auditor role having `role: "auditor"` on `KeyEntry` — auditor keys are not session-scoped.

This *does* argue for the Q6 middle path: if session grants are an agent-key mechanism, and auditor attestations bypass session grants entirely, then auditor keys are structurally different from actor keys. They don't participate in the session grant flow. The `role` field on `KeyEntry` captures this distinction without creating a separate primitive.

### What about Claude's session-identity observation (round 2, 7a)?

Claude correctly noted that the current design conflates "who are you" with "which session is this." The session grant mechanism naturally produces a `session_id`. The `DelegationChain` dataclass at `_types.py:67` already has this field. The mapping is:

- `session_id` in `on_behalf_of` = the session grant event's `event_id`.
- Wake events target a session by its `session_id`.
- Provenance events record `session_id` on every tool call.
- Session lifecycle: `session_grant` event creates it, `session_revocation` event terminates it.

This answers the routing question from `AGENTS.md` ("how does an event find the right session?"): the wake event carries a `session_id` that the harness adapter resolves to a native session handle.

### Code constraint: one signature or two?

**Option (b): the session grant is a separate signed event referenced by every action event.** This is the only option that doesn't change `sign_event()`'s return type. The action event is signed once (by the agent's key or, in v1, by the operator's HMAC key). The session grant is a separate event signed once (by the human's key). The verifier follows the reference chain.

This is the same pattern as the scope attestation (Q10): a configuration event that later events reference. Substrate's existing typed links (`LinkTypeDef` at `_types.py:370`) can express "action event references session grant" as a first-class relationship.

---

## 6. Q12: Temporal integrity

### Should `timestamp` be added to the signing envelope?

**Yes. Do it now.** The reasoning:

`_signing.py:build_signing_envelope()` (line 10-26) currently covers `event_id`, `work_item_id`, `actor_id`, `on_behalf_of`, `transition`, and `payload`. It does not cover `timestamp`. The timestamp is set at `_event_store.py:88` after signing.

This is a one-line addition to `build_signing_envelope()`:

```python
def build_signing_envelope(
    event_id: UUID,
    work_item_id: UUID,
    actor_id: str,
    transition: str | None,
    payload: dict | None,
    on_behalf_of: dict | None = None,
    timestamp: str | None = None,     # NEW
) -> bytes:
    envelope = {
        "event_id": str(event_id),
        "work_item_id": str(work_item_id),
        "actor_id": actor_id,
        "on_behalf_of": on_behalf_of,
        "transition": transition,
        "payload": payload,
    }
    if timestamp is not None:
        envelope["timestamp"] = timestamp
    return canonicalize(envelope)
```

The `timestamp` is optional — when `None`, the envelope has the same shape as before. The `verify_event()` function's existing backward-compat retry at `_signing.py:92-98` handles old envelopes that lack `timestamp`. This means:

- Old events without `timestamp` in the envelope continue to verify (backward compat).
- New events include `timestamp` in the envelope, making it integrity-protected.
- The migration is forward-only, matching BC-197's precedent for `on_behalf_of`.

**Is breaking backward compat acceptable?** Yes — the backward-compat path in `verify_event()` already handles exactly this case. There is no production deployment. The only existing signed events are test fixtures.

**The Postgres timestamp overwrite problem** (see section 2) means the timestamp in the envelope should be the *client-proposed* timestamp, not the DB-assigned one. The DB timestamp stays as the authoritative `Event.timestamp` (for ordering). The signed timestamp in the envelope is a *claim* by the signer about when the event was created. The RFC 3161 token anchors the DB timestamp. The ordering constraint below relates them.

### Relationship between three timestamps

Three timestamps exist:
1. `authenticated_at` in `on_behalf_of` — when the human authenticated.
2. `timestamp` in the event header — when the event was recorded (DB server clock).
3. RFC 3161 token timestamp — when the TSA attested to the event batch's existence.

**Ordering constraint:**

```
authenticated_at <= signed_envelope_timestamp <= event.timestamp <= TSA_timestamp + tolerance
```

Where:
- `signed_envelope_timestamp` is the timestamp in the signing envelope (new field from above).
- `event.timestamp` is the DB-assigned timestamp (existing field, not signed).
- `tolerance` accounts for clock skew between client and TSA (default: 5 minutes).

**Enforcement:** The verifier tool checks this ordering. Substrate does not enforce it at append time — that would require the TSA round-trip to be synchronous, which is too slow. The verifier flags violations as warnings (not errors) for the auditor to assess.

### Who provides the TSA?

**Configurable per-deployment.** V1 ships with a default list of free/public TSAs:
- FreeTSA (https://freetsa.org)
- DigiCert public TSA

The configuration is a list of TSA endpoints. The deployment picks one or more. For SOX, the auditor may require a specific TSA — the configuration supports this. The default is sufficient for the workplace-audit use case (internal compliance team).

### Batch interval for TSA tokens

**V1 default: 1 hour. Configurable.**

Rationale:
- Per-event is too expensive (HTTP call per event, ~50ms latency each).
- Sub-minute is unnecessary for workplace audit.
- Hourly is CloudTrail's digest interval — the structural analog from `agent-provenance/README.md:24`.
- Configurable as `{"tsa_batch_interval_seconds": 3600}` in substrate config.

The batch anchors a range of event sequences. The `timestamp_anchor` event (from BC-198's acceptance criteria) carries:
```
{
  "event_seq_start": 1000,
  "event_seq_end": 1047,
  "tsa_token": "<base64 RFC 3161 Token>",
  "tsa_url": "https://freetsa.org/tsa",
  "merkle_root": "sha256:..."  // hash of all events in range
}
```

---

## 7. Q13: Trust-root bootstrap

### V1 bootstrap mechanism

**Manual key-exchange ceremony for v1, with three options documented for v2.**

For the peer model (settled position Q3), the bootstrap is:
1. Operator generates identity keypair (ed25519, once BC-196 lands).
2. Operator publishes the public key fingerprint through a channel the auditor trusts.
3. Auditor records the fingerprint in their verifier configuration.

The "channel the auditor trusts" is deployment-specific. V1 supports one mechanism:

**Manual ceremony:** Operator and auditor meet (in person, video call, or signed email from a known address). Operator reads their public key fingerprint. Auditor records it. This is the same bootstrap used by PGP key signing and SSH `known_hosts` — low-tech, well-understood, and sufficient for the "solo + auditor" scenario where both parties are in the same organization.

The ceremony produces a signed `key_declaration` event in the substrate log:

```
key_declaration event:
  transition: "key_declaration"
  actor_kind: "system"
  payload:
    principal_id: "alice"
    public_key_fingerprint: "sha256:abc123..."
    attestation_type: "key"
    declared_at: "2026-05-23T14:00:00Z"
    method: "manual_ceremony"
  on_behalf_of:
    principal_id: "key:sha256:abc123..."
```

This event is signed by the declared key (self-referential). An auditor verifying the log starts from this event and follows the rotation chain. The `method` field documents how the bootstrap was performed — auditors can assess whether the method is adequate for their threat model.

### For the solo-dogfood case

**Bootstrap is a no-op.** The operator and auditor are the same person. The operator generates a keypair, signs events, and verifies their own log. The documentation contract:

> "Solo-dogfood mode provides integrity verification (events are signed and tamper-evident) but not authenticity verification (the same person signs and verifies). This is adequate for personal use and development. It is inadequate for arms-length audit where the auditor and operator are different parties. For arms-length audit, perform a key-exchange ceremony documented under 'Trust-root bootstrap' and record the resulting `key_declaration` event in the log."

### Is the bootstrap mechanism itself a signed event?

**Yes — the `key_declaration` event above.** This is critical for key rotation history. When an auditor joins late (e.g., a new compliance team member), they need to know:
1. What key was active at time T.
2. What key rotation events occurred.
3. That each rotation was signed by the previous key (or by a recovery key).

The `key_declaration` event + rotation events form a chain that starts at substrate initialization and extends to the present. The auditor verifies the entire chain from the initial `key_declaration` forward.

### V2 options (documented, not implemented)

- DNS TXT record binding key fingerprint to domain.
- GitHub SSH-key directory mapping.
- Enterprise SSO certificate chain.
- A separate signed key-registry artifact (like a minified Sigstore TUF root).

---

## 8. Q14: Identity continuity across key rotation

### Is the opaque ID stable or does it change?

**Stable.** The opaque ID (e.g., `"alice"`) stays constant across key rotations. The key fingerprint changes. The binding between them is a chain of signed rotation events in the log.

This is the correct design because:
- `_contract.py:590` already validates `principal_id` as a bare string with no format constraint. If `principal_id` were a key fingerprint, it would change on every rotation, and historical events would carry dead identifiers with no forward reference.
- The `on_behalf_of.principal_id` at `_types.py:66` is the stable identity. The `key_id` on the `Event` at `_types.py:99` is the current signing key. They are different fields for a reason.

### Where is the binding stored?

**In the substrate event log, as signed rotation events.** Not in an external registry, not in an IdP, not in a DID document.

The rotation event:

```
key_rotation event:
  transition: "key_rotation"
  actor_kind: "system"
  payload:
    old_key_fingerprint: "sha256:abc123..."
    new_key_fingerprint: "sha256:def456..."
    principal_id: "alice"
    rotated_at: "2026-05-23T14:00:00Z"
    rotated_by: "primary"   # or "recovery"
  on_behalf_of:
    principal_id: "key:sha256:abc123..."  # signed by old key
```

The verifier follows this chain:
1. Find the initial `key_declaration` event for `principal_id: "alice"`.
2. Find all subsequent `key_rotation` events for `principal_id: "alice"`.
3. Verify each rotation is signed by the old key (or the recovery key).
4. Build a timeline: `alice` was `sha256:abc123...` from T1 to T2, then `sha256:def456...` from T2 onward.
5. When verifying historical events, check the event's `key_id` against the timeline.

### What does an auditor see?

The auditor sees the full rotation chain. Events signed by `sha256:abc123...` are valid for the period where `alice`'s active key was that fingerprint. Events signed by `sha256:def456...` are valid after the rotation. The verifier tool automates this timeline reconstruction.

### Does this push us back toward DIDs?

**No.** The rotation chain mechanism we're building is simpler than DID's rotation/history mechanism. DIDs add:
- DID document resolution (network calls).
- Verification method arrays with controller relationships.
- A service endpoint discovery layer.

Substrate's rotation chain is: a linked list of signed events in the log, verifiable offline, no network calls. It achieves the same identity-continuity property without the DID spec's surface area. The DID ontology is battle-tested but overkill for a system where the log *is* the registry.

The `principal_id` field at `_contract.py:590` accepts any string. A future deployment could use `did:key:z6Mku...` as the `principal_id` if they want DID semantics. Substrate doesn't need to understand it — the deployment's verifier does the resolution. This is the "attestation_type" extensibility from settled position Q2/A.

---

## 9. Last bite at the apple

### Q5 (strict attribution): one sharpening

The locked position says "every tool call attributed to the harness operator." This is correct for v1. But the session grant topology from Q11 introduces a subtlety: when v2 lands, "strict attribution" means the *human* who issued the session grant, not the harness operator. The verifier must follow the session grant chain to attribute to the human, not to the agent.

The current locked position is fine for v1 (where there is one key). But the design doc should note that "strict attribution" has a v1 meaning (operator's key) and a v2 meaning (human's key via session grant). Otherwise, the v2 implementation will be ambiguous about which key the attribution follows.

### No other objections

The remaining ten locked positions are well-grounded in the code and use case. The consensus on Q1 (identity in substrate), Q3 (peer model), Q4 (trigger + actor), Q8 (per-user allowlists), and Q9 (single-user as degenerate case) is particularly sound — all compose naturally with the substrate code as it exists.

---

## 10. Anything still missed

### The `key_id` integrity gap

Every prior reviewer missed this, and it is load-bearing. `_signing.py:build_signing_envelope()` (line 10-26) covers `actor_id`, `on_behalf_of`, `transition`, and `payload`. It does **not** cover `key_id`. The `key_id` is set at `_event_store.py:72` from `key_entry.key_id` after signing. An attacker with write access to the event store can change `key_id` on any event without breaking the signature.

Under HMAC, this is moot (the attacker who has the HMAC secret can forge everything). Under asymmetric signing (BC-196), this becomes a real vulnerability: the event is signed by key A, but an attacker changes `key_id` to B. The verifier looks up key B, finds a valid public key, and the signature fails — but the failure mode is confusing (signature mismatch, not a clear forgery).

**Fix:** Add `key_id` to `build_signing_envelope()`. This is the same one-line change as adding `timestamp` (section 6). Both should land together.

### Algorithm transition strategy

BC-196 says HMAC stays as the default. BC-198 Layer 1 works with HMAC. But the design doc has no explicit strategy for the transition from HMAC to Ed25519:

- Do events signed with HMAC and events signed with Ed25519 coexist in the same log?
- When the operator switches from HMAC to Ed25519, what happens to the HMAC key?
- Does the verifier need to support both algorithms simultaneously?

The `alg` discriminator from BC-196's acceptance criteria (line 106-108) answers the first question (yes, they coexist). The `verify_event()` function's backward-compat retry at `_signing.py:92-98` shows the pattern for handling schema evolution. But the transition strategy — when to deprecate HMAC, how to phase it out — should be documented before implementation starts.

---

## Summary of positions

| Question | Position |
|----------|----------|
| Q6 (auditors) | **(c) Middle path.** `role` field on `KeyEntry`, constrained by `ActorRole`. Auditor keys can only sign attestation events. Same identity primitive, different signing scope. |
| Q10 (scope attestation) | **Versioned scope attestation events.** Signed by operator's key at init and on change. Harness heartbeats every N minutes for completeness. `session_id` in `on_behalf_of` links events to scope. |
| Q11 (agent vs. human key) | **Design schema now, implement in v2.** Session grant as separate signed event referenced by `on_behalf_of.session_id`. `sign_event()` signature unchanged. Agent signs events, human signs session grant, verifier checks both. |
| Q12 (temporal integrity) | **Add `timestamp` to signing envelope now.** RFC 3161 tokens hourly (configurable). TSA configurable per-deployment, default: FreeTSA + DigiCert. Ordering: `authenticated_at <= signed_ts <= event_ts <= TSA_ts + tolerance`. |
| Q13 (trust-root bootstrap) | **Manual key-exchange ceremony for v1.** `key_declaration` event in the log. Solo-dogfood is a documented no-op. Three v2 options documented. |
| Q14 (identity continuity) | **Stable opaque ID, rotation chain in the log.** `principal_id` stays constant. `key_id` changes. Verifier follows rotation events. No DIDs. |
| Missed: `key_id` integrity | **Add `key_id` to signing envelope alongside `timestamp`.** |
| Missed: algorithm transition | **Document HMAC-to-Ed25519 coexistence strategy before implementation.** |
