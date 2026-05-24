# Final-round design review — identity and multi-user
**Reviewer:** Kimi-K2.6-Turbo (round 3)  
**Date:** 2026-05-23  
**Scope:** Close the design space before implementation planning.

---

## 1. Pre-reading attestation

| File read | Most surprising finding |
|-----------|------------------------|
| `design/identity-and-multi-user.md` | The document contains *three* contradictory answers to Q2 (identifier format) — "Proposed positions" says opaque ID + attestation_type enum, "Agent positions" says raw key fingerprint, and the opencode reader input proposes `{id, key_fp, binding}` — yet the text never flags any of them as live disagreements. |
| `design/round-2-response.md` | The argument that "bundles-only" is sufficient and hybrid is unnecessary is too strong. In practice, if the solo path is the only one exercised during dogfooding, the bundle-export path will rot untested. |
| `design/identity-review-round2-mimo.md` | MiMo proposes adding `key_fp` directly to the signing envelope or to `on_behalf_of`, which makes the identifier-format decision more invasive to `_signing.py` than the consensus admits. |
| `design/identity-and-multi-user-review-deepseek-v4-pro.md` | The observation that BC-198 Layer 1 (RFC 3161 timestamping) is implementable today against the existing HMAC primitive is correct and under-exploited: temporal integrity does not wait for BC-196. |
| `design/review-kimi-k2.6-turbo.md` | The dissent on Q6 is the cleanest in round 2, but the proposed separate auditor primitive would require duplicating all of `_keys.py` infrastructure. |
| `design/review-qwen3.6-27b-identity-multi-user.md` | Qwen is the only reviewer who argues that SOX/HIPAA auditors *routinely* get direct DB access and therefore bundles are not required — a strong contrarian position that the bundle-consensus did not rebut with evidence. |
| `design/reviews/identity-and-multi-user-gemini-3.5-flash.md` | Gemini proposes harness heartbeats as a v1 feature. This is structurally sound but was not evaluated against the v1 complexity budget by any other reviewer. |
| `design/reviews/identity-and-multi-user-glm-5.1.md` | GLM-5.1 is the only reviewer who honestly engages with the DID argument for identity continuity across rotation, even while rejecting it. |
| `substrate/src/substrate/_signing.py` | The backward-compat retry at lines 93–98 proves the authors anticipated schema evolution, but the *missing* `timestamp` in the envelope is a gap that no amount of backward-compat logic fixes. |
| `substrate/src/substrate/_keys.py` | `KeyEntry` stores `secret: bytes` with no algorithm tag and no base64 decoding logic. This means an Ed25519 private key stored as a base64 string in the JSON key file would be silently corrupted by the UTF-8 coercion at lines 85–88. |
| `substrate/src/substrate/_types.py` | `ActorKind` enum exists (lines 13–16) but is never referenced in `DelegationChain`, `_signing.py`, or any delegation validation logic. The delegation chain cannot express "human delegating to agent" vs "agent to agent." |
| `substrate/src/substrate/_event_store.py` | `PostgresEventStore.append()` (lines 313–330) overwrites the client-side `timestamp` with the database server's `RETURNING timestamp`. The client-side `now` at line 88 is discarded in the Postgres path, making the DB server clock the de-facto timestamp — but it is still not in the signed envelope. |
| `substrate/src/substrate/_contract.py` | `validate_delegation_chain()` (lines 582–613) validates `principal_id`, `scope`, and `authenticated_at`, but `session_id` — a field in the `DelegationChain` dataclass — is completely absent from validation. Schema drift between `_types.py` and `_contract.py`. |
| `substrate/breadcrumbs/resolved/196-hmac-symmetric-no-external-verifiability.md` | The non-goal at line 125 says HMAC stays the zero-config default *indefinitely*. This means the `alg` discriminator must support HMAC-asymmetric mixed logs forever, not just during a migration window. |
| `substrate/breadcrumbs/resolved/197-no-delegation-chain-agent-actor-cannot-bind-to-human.md` | Status is "implemented" but acceptance criteria still have unchecked boxes for "per-principal signing keys" and "external auth-provider integration." The implementation is structural, not cryptographic. |
| `substrate/breadcrumbs/resolved/198-no-defense-against-operator-forgery.md` | Layer 1 is explicitly implementable against the existing HMAC primitive (line 189). This means the temporal-integrity fix (Q12) can land before asymmetric signing. |
| `agent-provenance/README.md` | §2's requirement that "the scope itself must be a signed first-class artifact in the event log" is described as "non-negotiable for v1 credibility," yet it is entirely absent from the nine design questions. |

---

## 2. Substrate code findings beyond what prior reviewers caught

Three gaps that prior reviews either missed or did not trace to a concrete line.

### 2.1 `session_id` is validated nowhere

`_contract.py:582-613` `validate_delegation_chain()` checks `principal_id` (line 590), `scope` (line 596), and `authenticated_at` (line 608). It never checks `session_id` despite it being a declared field in `_types.py:66` `DelegationChain`. This is a schema drift: the dataclass promises a field that the contract validator ignores. If `session_id` is intended to be load-bearing for Q11, the validator must reject non-string values before v1 ships.

### 2.2 `_keys.py` silently corrupts base64-encoded secrets

`_keys.py:85-88`:
```python
secret = entry["secret"]
if isinstance(secret, str):
    secret = secret.encode("utf-8")
```

For HMAC, a hex string or random ASCII password works fine. For Ed25519 (BC-196), the private key is 32 raw bytes. If a user stores it as a base64 string in the JSON key file — the natural way to put binary in JSON — this code UTF-8 encodes the base64 text rather than decoding it. The resulting bytes are the ASCII characters of the base64 string, not the 32 key bytes. `compute_hmac()` would not notice (it just needs bytes), but an Ed25519 signing call would fail or produce an unverifiable signature. BC-196 needs a `KeyEntry` format change that includes an explicit encoding field (e.g., `"encoding": "base64"`) or a dedicated `private_key_bytes` loader.

### 2.3 Postgres path discards the client timestamp

`_event_store.py:88` sets `now = datetime.now(UTC)` and uses it to construct the initial `Event`. However, `PostgresEventStore.append()` (lines 260–266) omits `timestamp` from the INSERT column list, letting the database default apply. It then returns the DB-generated timestamp at line 323 and constructs a new `Event` with it. The client-side `now` is thrown away. Prior reviewers noted that the timestamp is set after signing; they missed that in the Postgres path it is set by the *database server*, not the client, and still not signed. This actually makes the timestamp slightly harder for the operator to forge (they need DB admin access, not just the HMAC key), but it is still not cryptographically bound.

---

## 3. Q6 (reopened): Auditors

**Position: (c) the middle path — add `role` to `KeyEntry`.**

### Why not (a) the original consensus

The consensus says "auditors are just another identity." This overloads the actor primitive with an access-control concern that has a different threat profile. An auditor key compromise corrupts the verification layer itself — forged attestations can whitewash a malicious log segment. The code should be able to reject tool-call events signed by keys whose role is `"auditor"`, but the original consensus provides no hook for this.

### Why not (b) the Kimi/GLM dissent

The dissent proposes a completely separate primitive: separate keyring, separate verification path, bundle co-signatures instead of live-log events. Substrate has zero code for a second keyring. `_keys.py:23` `KeySet` loads exactly one JSON file with hot-reload, lifecycle states, and status validation. Duplicating this for auditors means duplicating `_load()`, `get_key()`, `verify_key_status()`, and the backward-compat retry logic in `_signing.py`. That is more implementation risk and more test surface than adding one field.

### Why (c) wins on code

`_keys.py:17-20`:
```python
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
```

Add `role: str = "actor"`. This is one line. `_contract.py` already has the pattern: `ActorKind` enum (line 13) and `validate_actor_kind()` (line 67) reject unknown values. A `validate_key_role()` following the same pattern costs ~5 lines. The signing envelope in `_signing.py` does not change because `key_id` already identifies the signing key; the verifier resolves `key_id` → `KeyEntry` via `KeySet.get_key()` (line 135) and checks `entry.role` before accepting the event type.

Concrete enforcement:
- `tool_call` events: verifier rejects if `entry.role != "actor"`.
- `auditor_attestation` events: verifier rejects if `entry.role != "auditor"`.
- Rotation and recovery events: accept `role ∈ {"actor", "auditor", "recovery"}`.

This keeps one key registry, one signing path, one hot-reload mechanism, and adds exactly one validation gate. It satisfies the security concern (auditor keys cannot sign actions) without the implementation tax of a parallel infrastructure.

---

## 4. Q10: Scope attestation

**Concrete proposal: a `scope_attestation` event chain, signed by the operator, with versioned evolution.**

### Schema

```json
{
  "event_id": "<uuid>",
  "actor_id": "alice",
  "transition": "scope_attestation",
  "payload": {
    "scope_id": "<uuid>",
    "previous_scope_id": "<uuid> | null",
    "effective_from": "2026-05-23T14:32:00Z",
    "harnesses": [
      {
        "name": "claude-code",
        "version": "0.x.y",
        "config_digest": "sha256:...",
        "session_scope": ["edit", "read", "bash:safe-subset"]
      }
    ],
    "operator_principal_id": "alice"
  }
}
```

### Who signs

The **operator** (the human who initialized the substrate instance, using their active signing key). This is the same `actor_id` that operates the harness. The attestation is a first-class event going through `sign_event()` at `_signing.py:43` and `append_event()` at `_event_store.py:43`.

### Versioning

**Scope is versioned as an append-only chain.** Every scope change (new harness added, harness removed, scope narrowed) emits a new `scope_attestation` with a new `scope_id` and a back-reference to `previous_scope_id`. The chain is stored in the log alongside tool-call events. An auditor verifying completeness walks the chain to know, for any point in time, which harnesses were in scope. Events from a harness not listed in the then-current scope are out-of-scope by definition.

### Scope evolution

**A new attestation per change, analogous to key rotation.** Do not mutate an old attestation. The chain is:
```
scope_attestation_v1 → scope_attestation_v2 → scope_attestation_v3
```
Each signed by the operator key at the time of change. The verifier accepts that the operator may add harnesses mid-period; the auditor's completeness check is "for the time range under audit, was every event's harness listed in the active scope attestation at that time?"

### Harness heartbeats

**Not in v1.** Gemini proposed periodic signed heartbeats from configured harnesses so an offline verifier can detect gaps. This is structurally sound but requires the harness to hold a signing key and emit periodic events. That is exactly the Q11 (agent-key) topology, which v1 does not implement. Defer heartbeats to v1.5, after the session-grant mechanism exists.

---

## 5. Q11: Agent-key vs. human-key topology

**Highest-priority new question. Position: design the schema to permit the two-key topology in v2; do not implement it in v1.**

### v1 scope

`_signing.py:43-57` `sign_event()` takes exactly one `key: bytes` and returns one signature. Changing this to two-key verification would require either:
- A multi-signature return type (breaking change to the event schema), or
- A session-grant reference that the verifier resolves externally (no envelope change).

**Choose the second path for v1.** Add one field to `on_behalf_of`: `grant_event_id: str | None`. This is a UUID reference to a separate `session_grant` event in the log. The agent signs action events with its session key; the verifier checks the action signature against the agent key, then loads the `grant_event_id` event and checks its signature against the human key. This requires **zero changes** to `build_signing_envelope()`, `sign_event()`, or `verify_event()` in v1.

### Session grant schema (v1 hook)

Extend `DelegationChain` in `_types.py:65`:
```python
@dataclass(frozen=True)
class DelegationChain:
    principal_id: str
    session_id: str | None = None
    authenticated_at: str | None = None
    expires_at: str | None = None          # NEW
    grant_event_id: str | None = None      # NEW
    scope: list[str] | None = None
```

Add corresponding validation in `_contract.py:582` (following the existing `authenticated_at` check at line 608). The `grant_event_id` is validated as a UUID string.

The session grant event itself is a first-class signed event:
```json
{
  "transition": "session_grant",
  "actor_id": "agent-session-abc123",
  "on_behalf_of": {
    "principal_id": "alice",
    "session_id": "agent-session-abc123",
    "authenticated_at": "2026-05-23T14:32:00Z",
    "expires_at": "2026-05-23T22:32:00Z",
    "scope": ["edit", "read"]
  }
}
```
Signed by the **human's long-lived key** at session start. In v1, this event is self-attested (same HMAC key signs everything), so the cryptographic separation is not yet real. But the schema is ready.

### Lifetime

**Time-bounded, scope-bounded, and revocable.** The `expires_at` field gives the lifetime. Revocation is a `session_revoked` event signed by the human key, referencing the original `grant_event_id`. The verifier checks: grant exists → grant not revoked → grant not expired → scope covers the action.

### Interaction with Q6 and session identity

This topology reinforces the Q6 middle path. The human key has `role="human"`, the agent session key has `role="agent"`, the auditor key has `role="auditor"`. The `session_id` field in `on_behalf_of` (currently unused, as noted in §2.1) is populated with the session UUID. This is how Claude's round-2 observation about session identity (section 7a) ties together: the session grant *is* the session identity.

### Two-key verification: option (a) vs (b)

**Option (a): the session grant is a separate signed event referenced by every action event.** This is the right choice because it requires no change to the single-signature envelope. Option (b) (both signatures in the event envelope) would require changing `sign_event()` return type, `Event.signature` field, and `verify_event()` logic — a cross-cutting schema change that should wait for BC-196's pluggable signer interface.

---

## 6. Q12: Temporal integrity

**Concrete proposal: add `timestamp` to the signing envelope now, reorder `_event_store.py` to sign the timestamp, and implement RFC 3161 batch anchoring as a configurable opt-in.**

### Add `timestamp` to the signing envelope

Yes. Change `build_signing_envelope()` in `_signing.py:18-26`:
```python
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

This breaks backward compatibility with all existing signed events. **Acceptable:** there is no production deployment, and the dogfood events can be regenerated. Add a backward-compat retry in `verify_event()` at line 93: if verification fails with timestamp, retry without it. This is the same pattern already used for `on_behalf_of`.

### Reorder `_event_store.py` to sign the timestamp

In `append_event()` (lines 43–108), move `now = datetime.now(UTC)` to **before** the `sign_event()` call, pass `now` into `sign_event()`, and include it in the `Event` construction. For the Postgres path, **include `timestamp` in the INSERT columns and values** (lines 260–266) so the stored timestamp matches the signed timestamp. Remove reliance on the database default for this column.

### Relationship between three timestamps

| Timestamp | Source | Meaning |
|-----------|--------|---------|
| `authenticated_at` | `on_behalf_of`, caller-provided | When the human authenticated to authorize the session. |
| `event.timestamp` | Client-generated, now in signed envelope | When substrate recorded the event. |
| `TSA token timestamp` | External RFC 3161 TSA | When the batch was anchored externally. |

**Ordering constraint:** `authenticated_at <= event.timestamp <= TSA_timestamp + tolerance`. **Enforced by the verifier tool, not substrate's write path.** Substrate should not reject events for verifier-side policy violations. The verifier emits a warning or error in its report if the constraint is violated. Tolerance default: 300 seconds.

### Who provides the TSA?

**Configurable per-deployment; v1 ships with no default TSA.** The config accepts a list of TSA URLs. The verifier tool accepts a list of trusted TSA root certificates. Document FreeTSA (`http://timestamp.freetsa.org`) and DigiCert public as examples. For SOX, the auditor may require their own TSA; the verifier must accept any configured TSA. Default behavior without config: no timestamp anchoring (Layer 0).

### Batch interval

**v1 default: hourly.** Configurable from 5 minutes to 24 hours. The batch is a `timestamp_anchor` event type (BC-198 acceptance criteria) covering all events since the previous anchor. Hourly is coarse enough to be cheap (~free with public TSAs) and fine enough for workplace audit. The batch interval is a deployment config, not a protocol constant.

---

## 7. Q13: Trust-root bootstrap

**Concrete proposal: a signed `bootstrap_attestation` as the first event in every log, plus a documented manual key-exchange ceremony.**

### v1 bootstrap mechanism

For the peer model, there is no automatic trust root without circularity. The operator and auditor conduct a manual out-of-band exchange (video call, signed email, corporate LDAP verification). After exchange, the operator writes a `bootstrap_attestation` event as the first event in the substrate log:

```json
{
  "transition": "bootstrap_attestation",
  "actor_id": "alice",
  "payload": {
    "operator_principal_id": "alice",
    "operator_public_key_fp": "sha256:abc123...",
    "auditor_principal_id": "auditor-bob",
    "auditor_public_key_fp": "sha256:def456...",
    "verification_channel": "corporate-ldap-verified-2026-05-23",
    "bootstrap_at": "2026-05-23T10:00:00Z"
  }
}
```

Signed by the operator's active key. This event is the root of trust for all subsequent key rotation events. An auditor receiving a bundle later can verify this event to see what public key the operator declared at initialization, and trace the rotation chain from there.

### Solo-dogfood case

Bootstrap is a **no-op** for solo use. The operator is the auditor. The documentation contract must state explicitly: "Solo verification skips bootstrap because the trust root is the same person. This path is adequate for dogfooding but is **not sufficient for arms-length audit**. For arms-length audit, follow the manual bootstrap ceremony and exchange public keys through a channel the operator does not control."

### Is the bootstrap mechanism itself a signed first-class event?

**Yes.** The `bootstrap_attestation` is a signed event with `transition="bootstrap_attestation"`. It is the first event in the log (event_seq 0 or 1). New auditors joining later verify this event to anchor the operator's declared public key. All subsequent rotation events reference back to this bootstrap fingerprint, making key history verifiable for late-joining auditors.

### Deployment-specific shortcuts

DNS TXT records, GitHub SSH keys, and enterprise SSO certificates are valid shortcuts for specific deployments, but they are **optional optimizations**, not the v1 default. The manual ceremony is the universal fallback that works without infrastructure.

---

## 8. Q14: Identity continuity across key rotation

**Position: the opaque ID is a stable human-readable name (`"alice"`) that binds to a sequence of keys; the binding is stored as signed rotation events in the log.**

### Stable opaque ID vs. changing fingerprint

The `principal_id` is the stable opaque ID. The key fingerprint is the cryptographic anchor that changes on rotation. This separation means:
- `principal_id: "alice"` stays constant across rotation.
- `key_fp: "sha256:old..."` → `key_fp: "sha256:new..."` is recorded in a signed `key_rotation` event.

### Where the binding is stored

In the **substrate event log** as a chain of signed `key_rotation` events. Each event contains:
```json
{
  "transition": "key_rotation",
  "actor_id": "alice",
  "payload": {
    "old_key_fp": "sha256:abc...",
    "new_key_fp": "sha256:def...",
    "rotated_at": "2026-05-23T14:00:00Z",
    "rotation_reason": "scheduled"
  }
}
```

Signed by the **old key** (or the recovery key, if the old key is lost). The verifier follows the chain: `alice` → `key_v1` → `key_v2` → `key_v3`.

### Auditor verification of historical events

The **verifier tool** is responsible for following the rotation chain. Substrate's runtime `KeySet` only stores current keys; it does not need to resolve historical keys. When verifying an event signed by `alice-key-v1` against the current `alice-key-v2`, the verifier loads the rotation event linking v1→v2, verifies that the rotation is signed by v1, and then verifies the historical event against v1. This is verifier-side logic, not substrate runtime logic.

### Does this push us back toward DIDs?

**No.** GLM-5.1 raised the honest argument that DIDs have built-in rotation history. However, substrate *is* an append-only event log — the rotation history lives in the same native log as the events themselves. A DID would add an external discovery layer (DNS, HTTPS, DID document parsing) that substrate has no code for (`_keys.py` does local JSON file loads; there is no HTTP client or DID parser). Building a rotation chain in the log reuses substrate's existing primitives (events, signatures, replay) without importing a spec dependency. The DID ontology is battle-tested, but substrate's log ontology is sufficient and native.

---

## 9. Last bite at the apple

### Objection to Q2/A: "zero substrate schema change" is misleading

The locked position says URI-prefix encoding (`key:sha256:...`, `oidc:...`) requires zero substrate schema change because `principal_id` at `_contract.py:590` is already a bare string. This is true for *validation*, but it is false for *verification*.

`_signing.py:72` `verify_event()` takes a `key: bytes` parameter. If the `principal_id` is `oidc:alice@corp.com`, the verifier cannot resolve it to raw verification material without a registry abstraction. `_keys.py:135` `get_key()` does a local dict lookup by `key_id`; it cannot resolve an OIDC subject or DID URI. BC-196's acceptance criteria require an `alg` discriminator, but they do not require a principal-resolution abstraction.

**The locked position should be refined:** the on-the-wire identifier is an opaque string, but substrate's *verification path* will need a `PrincipalResolver` interface (analogous to the pluggable signer interface) before `oidc:` or `did:` identifiers are actually verifiable. The `key:` prefix is the only one that works with `_keys.py` as-is. The others are reserved for future deployment-specific resolver plugins. This does not change the wire-format consensus, but it corrects the "zero schema change" framing to "zero *contract* schema change, with a verification-side interface addition deferred to BC-196 implementation."

---

## 10. Anything still missed

### Q15: Multi-device users

Every prior reviewer assumes one key per user. In the workplace scenario, a human has a work laptop, a home desktop, and possibly a CI service account. If each device holds a separate keypair, substrate's `_keys.py:94` `active_key()` returns **exactly one active key**. There is no concept of "multiple active keys bound to the same `principal_id`."

This forces an operational choice:
- **Copy the same private key to all devices:** poor security; compromise of one device revokes all.
- **Each device is a separate `principal_id`:** breaks the identity model; the same human appears as `alice-laptop` and `alice-desktop`.

**The key registry needs to support `principal_id` → list of active keys**, not just `key_id` → `KeyEntry`. `_keys.py:94` `active_key()` would need to become `active_keys_for(principal_id)` or the `KeySet` needs a linking table. This is a real schema change that v1 should at least hold a hook for: add `principal_id: str` to `KeyEntry` so that future versions can index by principal without a migration.

### Q16: Event log compaction and archival

An append-only log grows forever. Workplace audits have retention requirements (e.g., SOX: 7 years), but beyond that, events may be destroyed. None of the reviewers discuss how to compact, archive, or truncate the log without breaking the signature chain. If the verifier reconstructs a Merkle tree over the full log, the tree includes every event since genesis. To support truncation, the system needs **checkpoint events**: periodic signed Merkle roots that serve as new trust anchors, allowing an auditor to verify from the most recent checkpoint rather than from genesis. This is implied by BC-198 Layer 2 but was not called out as a storage/retention concern.

---

*End of review.*
