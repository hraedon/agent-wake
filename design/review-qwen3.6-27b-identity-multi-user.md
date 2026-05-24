# Identity and multi-user — second-round review

**Reviewer:** qwen3.6-27b (local)
**Date:** 2026-05-23
**Source doc:** `design/identity-and-multi-user.md`

---

## 1. Pre-reading attestation

| File | Most surprising finding |
|------|------------------------|
| `design/identity-and-multi-user.md` | BC-197 is **already implemented** (resolved, Plan 010), not "on the roadmap" as the design doc's intro implies. The `on_behalf_of` field is live in every event. |
| `substrate/src/substrate/_signing.py` | Purely HMAC. `build_signing_envelope` (line 10) already includes `on_behalf_of` in the signed canonical envelope — the delegation chain is integrity-protected by the HMAC. No asymmetric signing anywhere. |
| `substrate/src/substrate/_keys.py` | `verify_key_status()` (line 166) rejects revoked keys but has **no timestamp** — a key marked `revoked` makes all past events unverifiable, not just future ones. The design doc assumes a `revoked_at` capability that doesn't exist. |
| `agent-provenance/README.md` | BC-198 (operator forgery) is a separate concern from BC-196/197 that the design doc barely mentions. The trust model's 6-layer progression (HMAC → delegation → RFC 3161 → asymmetric → witness → OpenTimestamps) is the real constraint on identity choices. |
| `substrate/breadcrumbs/resolved/197-...md` | `validate_delegation_chain` in `_contract.py:582` validates `principal_id` as `str` with **no format constraint**. It accepts `"alice"`, `"sha256:abc"`, `"did:key:z6Mku..."` indistinguishably. |
| `substrate/breadcrumbs/README.md` (BC-196 entry) | Still open. Resolution says "pluggable signature scheme (WS-1/WS-3 in Plan 008)" is the prerequisite. No Ed25519 code exists. |
| `substrate/breadcrumbs/resolved/198-...md` | Layer 1 (RFC 3161 TSA) is "implementable today against the existing HMAC primitive" — meaning the identity format choice doesn't block timestamping. |

## 2. Substrate code findings

### Already supported (design doc treats as future)

- **Delegation chain (`on_behalf_of`)** is fully implemented. `_types.py:65` has the `DelegationChain` dataclass with `principal_id`, `session_id`, `authenticated_at`, `scope`. Wired through `transition()`, `create_work_item()`, `append_event()`, the sidecar API, replay, and InMemory backend. 31 tests. BC-197 resolved.
- **Key lifecycle states** (`active`, `deprecated`, `revoked`) in `_keys.py:72-78`. `verify_key_status()` warns on deprecated, raises on revoked. `KeySet` hot-reloads from JSON file with mtime polling.
- **`key_id` on every event** in `_types.py:99`. Enables per-key attribution.
- **`actor_kind` enum** (`AGENT`, `HUMAN`, `SYSTEM`) in `_types.py:13`. Built-in role taxonomy the design doc doesn't mention.
- **`actor_id` validation** (BC-205): non-empty, printable, max 255 chars.

### Not supported (design doc assumes)

- **No asymmetric signing.** BC-196 accepted but not implemented. `_signing.py` is exclusively HMAC-SHA256. The auditor model **cannot work** — the auditor would need the same secret as the signer.
- **No `revoked_at` timestamp on keys.** `_keys.py` has no temporal dimension to revocation. Marking a key `revoked` breaks verification of all events signed with it, including pre-revocation ones.
- **No key generation.** `_keys.py` loads from JSON; no `keygen` command exists.
- **No per-actor key resolution.** `KeySet` is a single shared keyring. No mapping from `actor_id` to which key that actor uses. Current model: "one keyring, one active key, everyone signs with it."

## 3. Question A: Identifier format on the wire

**Position: Opaque ID + attestation-type enum.**

`_contract.py:582` already accepts `principal_id: str` with zero format constraints. The `DelegationChain` type in `_types.py:65` is a thin wrapper over `dict | None`. The on-the-wire identifier is already opaque.

### Substrate changes per option

**Raw key fingerprint (`sha256:abc123...`):**
- Minimal change to `_signing.py` — `actor_id` at line 12 is already `str`.
- But `_keys.py` would need to become a public-key store. `KeyEntry.secret` is `bytes` (HMAC key). For Ed25519, need `public_key` and `private_key`. This is the BC-196 change regardless of identifier format.
- Hard to extend: enterprise OIDC binding later forces a new field or naming convention hack.

**Opaque ID + attestation-type enum:**
- Add `attestation_type: str | None` to `DelegationChain` in `_types.py:65`. 5-line change.
- Add validation in `_contract.py:582`. ~10 lines.
- BC-196's pluggable signing adds `signing_scheme: str` to key entry, mapping to attestation type.
- Registry that maps `principal_id` to verification material is a deployment component, not substrate.
- **Least invasive.** The signing envelope in `_signing.py:18` carries `on_behalf_of` as `dict | None` — a new key doesn't change the canonicalization path.

**DID URI:**
- Requires substrate to understand DID resolution: parse URI, extract method, fetch verification material over HTTPS, parse DID Core document, manage TTL.
- `_keys.py` `KeySet` model (JSON file, hot-reload) doesn't map to DID documents. Entirely new key resolution path.
- Most invasive. Adds spec dependency (DID Core 1.0+) and runtime network calls.

**Decision:** Opaque ID + attestation-type enum. Already structured this way in substrate. Preserves auditor model — the verifier tool reads the deployment-provided registry to resolve ID to public key.

## 4. Question B: Auditor access model

**Position: Hybrid. Direct verification is the v1 default; bundles are v2.**

The workplace-audit use case (`agent-provenance/README.md` §8): "the project owner wants to use AI agents at work in a regulated context."

### What real compliance regimes require

- **SOX:** Auditor gets direct access to system logs during audit period, verifies signatures, produces separate workpapers. No offline bundles required.
- **HIPAA 164.312(b):** "Mechanism that records and examines activity in information systems." Direct access to logs is the standard model.
- **SOC 2 / internal IT audit:** Auditor queries logs for audit period, verifies controls. Direct access.

None of these require offline bundles as the primary mechanism. Bundles are for: long-term retention after decommission, cross-jurisdictional audits where live access isn't possible, regulatory submissions requiring a signed artifact.

For v1 (solo + auditors), the auditor is in the same organization with read access to the same Postgres database. Direct verification is sufficient. The auditor's tool replays the event log, verifies signatures, and produces a report.

Hybrid is correct because the compliance story evolves: v1 ships direct verification, v2 adds signed bundles for arms-length scenarios. But bundles shouldn't be a v1 concern. `agent-provenance/README.md` §4 already describes the trust model as layered — direct verification is layer 1-2, bundles would be layer 3+.

**Sharp point from code:** `agent-provenance/README.md` §2 says the scope statement itself must be a signed artifact. This is completeness attestation, and it's harder than the access model. The auditor needs to know the scope at each point in time — a versioned scope attestation problem, not a one-time declaration.

## 5. Question C: Recovery mechanism (v2)

**Position: Second-device recovery key.**

### v1 schema constraints per option

**Second-device recovery key:**
- v1 needs: `KeySet` to support a `role: "primary" | "recovery"` field on `KeyEntry` (`_keys.py:17`). 3-line change.
- Recovery key signs a `key_rotation` event. Same mechanism the design doc's position 7 describes. No new substrate primitive.
- **Does not constrain any v1 decision.** Rotation event format works with or without recovery keys.

**Social recovery:**
- v1 needs: threshold-signing primitive. `verify_event()` in `_signing.py:72` takes a single `key: bytes`. Supporting co-signed recovery requires either: (a) new event type with signature array, or (b) Shamir secret sharing module.
- Both are invasive. Changes the rotation schema.
- **Constrains v1:** The `key_rotation` event format must accept multiple signatures.

**OIDC-only:**
- Works as side-channel if identifier is opaque (the IdP vouches for new key binding to same OIDC subject). Compatible with opaque ID approach.
- Creates two-tier system: enterprise users have recovery, solo users don't.
- **Does not constrain v1** but is inequitable.

**No recovery, ever:**
- Simplest. But if the owner loses their key during dogfooding, the audit log is permanently unverifiable.
- **Does not constrain v1** but makes v2 harder. If v1 events lack recovery key in schema, v2 can't recover v1 identities without log fork.

**Decision:** Second-device recovery key. Least v1 schema change, uses existing rotation mechanism, no two-tier system. Recovery key generated at `agent-wake identity init`, stored separately, signs rotation events. Verifier tries old key, then recovery key.

## 6. Disagreement with consensus

**Position 6: "Auditors are identities too" — I disagree.**

`_types.py:13` has `ActorKind` with `AGENT`, `HUMAN`, `SYSTEM`. An auditor signing an attestation would have `actor_kind = "HUMAN"`, indistinguishable from a regular human actor. But an auditor's attestation ("I reviewed this log segment") is semantically different from an actor's action ("I executed this tool call").

Why it matters:
1. An auditor's key should never sign action events (only attestations). Shared identity space allows an auditor key to sign a `tool_call` event — a policy violation.
2. Auditor key distribution is different. Actors publish keys for signing; auditors publish keys for verification. Mixed registry creates confusion.

**Proposed fix:** Add a role constraint (not a new identity primitive). The auditor's identity is the same `(id, key_fp)` pair, but with a constraint restricting their signing scope to attestation events. This is a validation rule, not a schema change.

## 7. Things the design doc misses

### A. BC-196 is the actual blocker, not identity format

The design doc spends significant ink on identifier format (Q2, Question A) but the auditor model cannot work until BC-196 lands. With HMAC, the auditor holds the same secret as the signer and can forge events. Identity format is irrelevant until asymmetric signing exists. Frame BC-196 as the critical path.

### B. Key compromise detection is not addressed

`_keys.py:166` `verify_key_status()` has no temporal component. If a key is compromised at time T, events before T are valid and events after T are not. Without `revoked_at`, the verifier cannot distinguish. The design doc's position 7 mentions "effective timestamp" but doesn't specify where it lives. It needs to be in `KeyEntry` (`_keys.py:17`) and persisted in the key file.

### C. `actor_id` vs `principal_id` vs `trigger_identity` mapping is missing

`_types.py:96` has `actor_id: str`. `_types.py:66` has `principal_id: str` in `DelegationChain`. The design doc discusses `trigger_identity` and `actor_identity` but doesn't map them to these fields. Three identities per event needs a clear field mapping.

### D. Event log export format for auditors

The verifier tool (`agent-provenance/README.md` §6) replays substrate events. But how does the auditor get them? Direct Postgres query? Substrate API? For solo + auditors, the auditor might not have Postgres credentials. A signed JSON export is needed for v1, even a simple one.

### E. Scope attestation is versioned, not one-time

`agent-provenance/README.md` §2 says the scope must be a signed artifact. But scope changes mid-period: if a new harness is added at 2pm, events from 9am-2pm were captured under a narrower scope. The auditor needs point-in-time scope. This is a versioned scope attestation problem. The design doc doesn't address scope evolution.
