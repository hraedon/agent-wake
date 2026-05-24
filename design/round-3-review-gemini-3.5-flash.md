# Final-Round Design Review — Identity and Multi-User
**Reviewer:** Gemini 3.5 Flash (Round 3)  
**Date:** 2026-05-23  
**Status:** Final positions before implementation planning  

---

## 1. Pre-Reading Attestation

I have thoroughly reviewed the design history, the substrate codebase, and the consumer requirements. The files and my primary findings are summarized below:

| File / Document | Surprising Finding |
| :--- | :--- |
| `design/identity-and-multi-user.md` | It treats delegation (BC-197) as an open gap, whereas `_contract.py:582` and `_types.py:65` already have concrete delegation dataclass structures and contract validators implemented. |
| `design/round-2-response.md` (Claude) | Claude's observation in §7a that "session" is currently conflated with "principal" and that wake events target sessions, while session lifecycle remains undefined in the identity schema. |
| `design/identity-review-round2-mimo.md` | MiMo's focus on putting the key fingerprint (`key_fp`) directly into the signing envelope or `on_behalf_of`, making the identifier-format choice highly invasive to `_signing.py`. |
| `design/identity-and-multi-user-review-deepseek-v4-pro.md` | Deepseek's realization that Layer 1 of BC-198 (RFC 3161 timestamping) is fully implementable today with existing HMAC code and doesn't need to wait for asymmetric keys (BC-196). |
| `design/review-kimi-k2.6-turbo.md` | Kimi's clean dissent on Q6 (auditor keys/role separation from actor keys to avoid token/access-control overloading) and suggestion that the v1 schema must include a recovery key role hook to prevent breaking changes in v2. |
| `design/review-qwen3.6-27b-identity-multi-user.md` | Qwen was the sole reviewer asserting that regulatory auditors (like SOX or HIPAA) routinely demand direct database access (read-only SQL login) rather than exports/bundles, contesting the AWS CloudTrail comparison. |
| `design/reviews/identity-and-multi-user-gemini-3.5-flash.md` | Gemini proposed active harness heartbeats in v1 as the primary defense against the "missing events" problem, which other reviews largely ignored or deferred due to complexity. |
| `design/reviews/identity-and-multi-user-glm-5.1.md` | Engaged deeply with the DID rotation story to show that DIDs are the only standard with built-in rotation and delegation history, even though rejecting them for v1 due to lack of network stack. |
| `substrate/src/substrate/_signing.py` | `verify_event` (lines 93–98) has a backward-compatibility loop specifically to verify old envelopes without `on_behalf_of` fields. |
| `substrate/src/substrate/_keys.py` | `KeyEntry` (lines 17–20) lacks a `public_key` field entirely, meaning that `_keys.py` currently only supports symmetric HMAC keys and must be modified for any asymmetric signature verification. |
| `substrate/src/substrate/_types.py` | `ActorKind` enum (lines 13–16) defines `AGENT`, `HUMAN`, and `SYSTEM`, but this enum is not used in any validation or signature verification logic. |
| `substrate/src/substrate/_event_store.py` | `PostgresEventStore.append()` (lines 260–266) completely discards the client-generated timestamp (`now` on line 88) and lets the database server generate the timestamp via a table default. |
| `substrate/breadcrumbs/resolved/196-*.md` | Non-goals explicitly declare that HMAC-SHA256 must remain the zero-config default indefinitely, meaning mixed HMAC/Asymmetric signature support is permanent. |
| `substrate/breadcrumbs/resolved/197-*.md` | Status is marked as "implemented" despite cryptographic delegation verification still being deferred to BC-196. |
| `substrate/breadcrumbs/resolved/198-*.md` | Confirms that RFC 3161 timestamping is Layer 1 and can be built on top of HMAC immediately. |
| `agent-provenance/README.md` | §2's insistence that a signed "scope statement" is non-negotiable for v1 credibility, yet it was completely missing from the original design doc's nine questions. |

---

## 2. Substrate Code Findings Beyond What Prior Reviewers Caught

Through deep analysis of the code, I have uncovered critical structural gaps that must be resolved prior to freeze:

### 2.1 The Postgres Clock Mismatch & Timestamp Bypass
`_event_store.py:88` sets `now = datetime.now(UTC)` before calling `sign_event()`. However, `PostgresEventStore.append()` (lines 260–266) omits `timestamp` from the INSERT column list, letting the database default apply. It then returns the DB-generated timestamp at line 323 and constructs a new `Event` with it. 

**The Implication:** Because the database server's clock generates the stored timestamp *after* signature, the signed timestamp and the stored database timestamp will always diverge. To include `timestamp` in the signing envelope (as proposed in Q12), we *must* modify the SQL INSERT statement in `PostgresEventStore.append` to write the client-generated timestamp rather than letting Postgres generate it via default.

### 2.2 Sequence-Number Mutability (Operator Forgery Vector)
In `_event_store.py:58`, `event_seq` is allocated before `sign_event` is called. However, `event_seq` is completely omitted from the signature envelope constructed in `build_signing_envelope()` (`_signing.py:18-25`).

**The Implication:** Because the signature does not commit to the sequence number, a malicious database administrator or operator can swap the sequence numbers of events (e.g., swapping event 2 and event 3) to change the chronological sequence of tools executed. The signatures will verify perfectly because neither envelope commits to its place in the sequence. To prevent sequence-swapping, `event_seq` *must* be added to the signature envelope.

### 2.3 Workflow Isolation Bypass (Replay Vector)
The signature envelope in `_signing.py:18-25` commits to `event_id`, `work_item_id`, `actor_id`, and payload details, but it does *not* commit to `workflow_name` or `workflow_version` (both are fields on the `Event` schema in `_types.py:100-101`).

**The Implication:** A malicious operator can take a signed event payload from one workflow and inject/replay it into a completely different workflow. The signature will verify perfectly. To prevent cross-workflow replays, the envelope must sign both `workflow_name` and `workflow_version`.

---

## 3. Q6 (Reopened): Auditors as Identities

I recommend **(c) the middle path — add `role` to `KeyEntry`**, and strongly reject (a) and (b).

### Why not (a) the original consensus?
Treating auditors as "just another identity" without distinguishing their role invites capability creep. If auditor keys use the exact same primitive without restriction, a compromised auditor key can be used to sign and record fake tool executions (`tool_call` events). Conversely, an actor key could sign an `auditor_attestation` event, self-certifying their own logs. Overloading the primitive without role constraints breaks the security model.

### Why not (b) the Kimi/GLM dissent?
Creating a completely separate keyring and verification path for auditors represents a massive implementation tax. `KeySet` (`_keys.py:23`) handles file reading, environment overrides, status caching, and hot-reloads. Duplicating this for a separate auditor primitive would duplicate test surface and code volume.

### Grounding (c) in the substrate code
We can implement robust role separation with a simple schema change in `_keys.py`:

```python
# In substrate/_keys.py:16-20
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
    role: str = "actor"  # Added role: 'actor' | 'auditor' | 'recovery'
```

In the verification flow, the verifier resolves the key and applies simple, rigid gating policies based on event types:
* **Tool calls / transitions:** Reject if `key_entry.role != "actor"`.
* **Auditor attestations:** Reject if `key_entry.role != "auditor"`.
* **Key rotation / recovery events:** Accept `role` in `{"actor", "auditor", "recovery"}`.

This gives us the exact security segregation the dissent wants without duplicating the key management codebase.

---

## 4. New Q10: Scope Attestation

To satisfy the non-negotiable workplace audit completeness requirement outlined in `agent-provenance/README.md` §2, I propose a formal **Scope Attestation event chain** committed directly to the event store.

### The Proposal
1. **Who signs:** The operator (the human authority) signs the scope statement using their active key during substrate/workspace initialization.
2. **Versioning & Representation:** The scope is represented as a first-class event in the log, using a dedicated transition type (`scope_attestation`). Every modification (adding or removing a harness, or editing a harness's permission level) is appended as a new event that contains a hash-link back to the previous scope attestation.
3. **Scope Rotation:** Just like key rotation, scope is immutable. If a harness changes, a new `scope_attestation` is committed. The verifier checks that every tool execution's harness configuration matches the active `scope_attestation` in effect at that point in the log's timeline.

```json
{
  "event_id": "<uuid>",
  "actor_id": "alice",
  "transition": "scope_attestation",
  "payload": {
    "scope_id": "<uuid>",
    "previous_scope_id": "<uuid> | null",
    "effective_from": "2026-05-23T19:48:57Z",
    "harnesses": [
      {
        "name": "claude-code",
        "version": "0.1.2",
        "config_digest": "sha256:d57e...",
        "allowed_tools": ["Read", "Edit", "Bash"]
      }
    ]
  }
}
```

### Harness Heartbeats (v1 vs. v2)
Harness heartbeats are **out of scope for v1**. Because v1 lacks asymmetric agent-specific keys (see Q11), a harness heartbeat would be signed with the shared operator key. A malicious operator could easily forge a harness heartbeat using the same key, defeating the security benefit. Heartbeats must wait for the v2 session-grant key topology.

---

## 5. New Q11: Agent-Key vs. Human-Key Topology

This is the most critical cryptographic boundary in the design. I propose: **design the schema hooks in v1 to support the two-key topology, but defer full cryptographic implementation to v2.**

### The Verification Architecture: Option (a) Wins
I strongly recommend **Option (a): the session grant is a separate signed event in the log, referenced by every action event.**
* **Why Option (a) wins on code:** Changing the low-level signing envelope (`_signing.py:43` `sign_event()`) to support a two-signature array would break the database schema and invalidate backward compatibility. 
* **The Flow:** Under Option (a), the agent signs events using its ephemeral session key. Every action event carries the `session_id` in its `on_behalf_of` metadata. The verifier verifies the signature against the agent's key, then looks up the `session_grant` event in the log matching the `session_id`. It then verifies that the `session_grant` event was signed by the human's long-lived identity key. This keeps `_signing.py`'s signature checks completely unmodified and lightweight.

```
┌──────────────────────────────────────┐
│  session_grant Event                 │
│  - session_id: agent_session_pubkey  │
│  - Signed by: human_identity_key     │
└──────────────────────────────────────┘
                  ▲
                  │  verifies human authority
                  │
┌──────────────────────────────────────┐
│  tool_call Event                     │
│  - session_id: agent_session_pubkey  │
│  - Signed by: agent_session_key      │
└──────────────────────────────────────┘
```

### Schema Hooks for v1
We must extend the `DelegationChain` in `_types.py:65-70` to hold the fields needed for the session grant:

```python
# In substrate/_types.py:65-70
@dataclass(frozen=True)
class DelegationChain:
    principal_id: str
    session_id: str | None = None
    authenticated_at: str | None = None
    expires_at: str | None = None          # NEW
    grant_event_id: str | None = None      # NEW
    scope: list[str] | None = None
```

We must also update `_contract.py:582` `validate_delegation_chain()` to validate these new fields as optional strings.

### Grant Lifetimes & Revocation
* **Lifetime:** Grants are time-bounded (e.g., 8 hours), scope-bounded (limited to specific tools/harnesses), and revocable.
* **Revocation:** The human commits a `session_revocation` event signed by their long-lived identity key. The verifier checks that the tool-call timestamp falls before the revocation event timestamp.

### Auditor Alignment
This topology aligns beautifully with Q6: the human key has `role="human"`, the agent key has `role="agent"`, and the auditor key has `role="auditor"`. Auditor attestations are signed by the auditor key directly, and do not reference any ephemeral session keys.

---

## 6. New Q12: Temporal Integrity

To prevent operators from backdating or altering event timelines, I propose the following:

### 1. Add `timestamp` to the Signing Envelope
Modify `_signing.py:18-25` to include the ISO-formatted event timestamp:

```python
# In substrate/_signing.py:18-25
envelope = {
    "event_id": str(event_id),
    "work_item_id": str(work_item_id),
    "actor_id": actor_id,
    "on_behalf_of": on_behalf_of,
    "transition": transition,
    "payload": payload,
    "timestamp": timestamp.isoformat(),  # NEW
}
```
**Backward Compatibility:** Add a fallback block in `_signing.py:93` `verify_event()` to retry verification without the `timestamp` field if the initial check fails, maintaining compatibility with existing dogfood logs.

### 2. Fix the Postgres Clock Bypass
Update `PostgresEventStore.append()` in `_event_store.py:260-266` to explicitly insert the client-generated `timestamp` column. This forces the stored database record to match the signed timestamp exactly.

### 3. Timestamp Relationship and Ordering Constraints
The three timestamps must follow a strict causal relationship:
```
authenticated_at <= event.timestamp <= TSA_timestamp + tolerance
```
**Enforcement:** This constraint must be enforced by the **offline verifier tool**, not the online substrate write path. If the timeline is violated, the verifier emits a high-priority warning in the audit report.

### 4. TSA Providers & Batching
* **Provider:** Configurable per-deployment via `SUBSTRATE_TSA_URL` config. No default TSA is hardcoded to remain offline-friendly.
* **Batching:** A default interval of **1 hour** is recommended. The system bundles events and commits a `timestamp_anchor` event containing the Merkle root of the hour's events, signed by the TSA.

---

## 7. New Q13: Trust-Root Bootstrap

To resolve the circularity of operators providing fraudulent public keys to auditors, we must establish a clear bootstrap mechanism.

### The Proposal
1. **The Event:** A signed `bootstrap_attestation` is committed as the very first event in the log (sequence 0):

```json
{
  "event_id": "<uuid>",
  "transition": "bootstrap_attestation",
  "actor_id": "alice",
  "payload": {
    "operator_principal_id": "alice",
    "operator_public_key_fp": "sha256:abc...",
    "auditor_principal_id": "bob-auditor",
    "auditor_public_key_fp": "sha256:def...",
    "verification_channel": "out-of-band-video-key-exchange",
    "bootstrap_at": "2026-05-23T19:48:57Z"
  }
}
```

2. **The Ceremony:** The operator and auditor conduct a manual out-of-band key exchange (e.g., video confirmation, GPG-signed email, or enterprise directory comparison).
3. **Verification:** The auditor's verifier tool reads the `bootstrap_attestation` at sequence 0. This event serves as the ultimate root of trust. All future key rotations and scope adjustments must trace their signature lineage back to the public keys declared in this bootstrap event.
4. **Solo-Dogfood Case:** For local dogfooding, the bootstrap event is auto-generated by the CLI, registering the solo operator as both operator and auditor. The documentation contract must explicitly state: 
   > [!WARNING]
   > Solo-mode verification skips independent bootstrap. This is acceptable for local development but cryptographically invalid for arms-length compliance audits.

---

## 8. New Q14: Identity Continuity Across Key Rotation

I strongly recommend keeping the **opaque ID stable** while storing key history directly in the log. DIDs are unnecessary and rejected.

### Stable ID with Signed Rotation Events
* The `principal_id` remains constant (`"alice"`).
* Key rotation is recorded as a `key_rotation` event signed by the *old key* (or the recovery key if the old key is lost).
* The verifier tool builds a timeline of valid public keys for each `principal_id` by playing the event log forward from the `bootstrap_attestation`.
* When verifying historical events, the verifier checks the event signature against the key that was active at the event's timestamp.

### Rejection of DIDs
We do not need DIDs. Substrate is already an append-only event log. Storing the rotation chain in the log itself completely avoids the need for external DID resolvers, HTTP clients, and document caching, keeping substrate lightweight and self-contained.

---

## 9. Last Bite at the Apple

### Refinement of Locked Q2/A Consensus
The locked position claims that OIDC/DID URI-prefixes require "zero substrate schema change" because `principal_id` is an opaque string. 

**My Correction:** While this is true for schema validation, it is **false for signature verification**. The low-level verifier `_signing.py:72` `verify_event()` takes a `key: bytes` parameter. If `principal_id` is `oidc:alice@corp.com`, substrate cannot resolve it to a raw verification key without a registry. 

Substrate's verification path *must* expose a pluggable `PrincipalResolver` interface (similar to the pluggable signer interface for BC-196). The `key:` prefix is the only one that works out-of-the-box with `_keys.py:135`. The others must be treated as reserved hooks for future resolver plugins.

---

## 10. Anything Still Missed

### Q15: Multi-Device Users
`_keys.py:94` `active_key()` assumes exactly one active key per key set. If a user works across a laptop, a desktop, and a CI system, they must either copy their private key across devices (a major security violation) or register as separate identities (e.g., `alice-laptop`, `alice-desktop`), breaking identity continuity.

**The Solution:** The `KeyEntry` schema must be extended to include `principal_id`:
```python
# In substrate/_keys.py
class KeyEntry:
    key_id: str
    principal_id: str  # NEW: allows mapping multiple active key_ids to one principal
    secret: bytes
    status: str
    role: str
```
This lets `KeySet` support multiple active keys bound to the same human principal.

### Q16: Event Log Compaction & GDPR Erasure (Article 17)
Cryptographic event chains grow forever, but compliance and GDPR require capability for deletion. If a verifier computes hashes over the full sequence from genesis, deleting an event to comply with a right-to-erasure request breaks the audit trail permanently.

**The Solution:** Introduce periodic `checkpoint` events. A checkpoint event contains a signed Merkle root of all state up to that point. This allows an auditor to archive/truncate events prior to the checkpoint while still validating the integrity of all subsequent events.

---
*End of Review.*
