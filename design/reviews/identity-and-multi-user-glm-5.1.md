# Design Review: Identity and Multi-User

**Reviewer:** glm-5.1 (zai-coding-plan/glm-5.1)
**Date:** 2026-05-23
**Document reviewed:** `/projects/agent-wake/design/identity-and-multi-user.md`

---

## 1. Pre-reading attestation

| File | Most surprising thing |
|------|----------------------|
| `/projects/agent-wake/design/identity-and-multi-user.md` | The "Proposed positions" section at line 292 converged on `Opaque ID + attestation_type enum` for Q2, but the "Agent positions" section at line 414 pivoted to raw key fingerprint. These two sections contradict each other and neither flags the contradiction. |
| `/projects/substrate/src/substrate/_signing.py` | `on_behalf_of` is a `dict | None` that flows through every signing path. The delegation chain schema is already baked into canonical JSON at line 22. Any identifier format change reflows through `build_signing_envelope()`. |
| `/projects/substrate/src/substrate/_keys.py` | `KeyEntry` (line 17) stores `secret: bytes` and `status: str` with no fingerprint field, no public key, no algorithm discriminator. The entire key lifecycle is HMAC-shaped. Asymmetric signing requires a fundamentally different `KeyEntry` structure. |
| `/projects/substrate/breadcrumbs/resolved/196-hmac-symmetric-no-external-verifiability.md` | BC-196 acceptance criteria (line 106) already specify "Event payloads carry an `alg` discriminator." This pre-decides part of Question A. |
| `/projects/substrate/breadcrumbs/resolved/197-no-delegation-chain-agent-actor-cannot-bind-to-human.md` | BC-197's `principal_id` field (line 111) is a bare string with no format constraint. The `validate_delegation_chain()` function in `_contract.py:590` just checks `isinstance(principal_id, str)`. The wire format question is entirely open in the code. |
| `/projects/substrate/breadcrumbs/resolved/198-no-defense-against-operator-forgery.md` | Layer 1 (RFC 3161 timestamping) is implementable before BC-196 lands (line 189). The auditor model does not strictly require asymmetric signing to be useful. The design doc misses this. |
| `/projects/agent-provenance/README.md` | Section 4 trust model (line 80) lists HMAC as tier 1 and Ed25519 as tier 4, with delegation chain as tier 2. The ordering implies identity ships before asymmetric signing. This constrains Question B. |

---

## 2. Substrate code findings

### Already supported that the design doc treats as future work

- `on_behalf_of` is a fully-schematized, signed, validated field across the entire event pipeline (`_signing.py:16`, `_contract.py:582`, `_types.py:108`, `_events.py`, `_event_store.py`, `_replay.py`, `_in_memory.py`, sidecar routes). BC-197 is status "implemented" in the breadcrumb index. The design doc discusses delegation as if it is future work; the code already has it.
- `validate_delegation_chain()` (`_contract.py:582`) enforces `principal_id` presence and type. It is a real schema constraint, not a comment.
- Key lifecycle states (`active`, `deprecated`, `revoked`) are enforced in `KeySet.verify_key_status()` (`_keys.py:166`). Hot-reload from file (`_keys.py:122`), env-var secret override (`_keys.py:80`). This is more than the design doc credits substrate for.

### Not supported that the design doc assumes

- `KeyEntry` (`_keys.py:17`) is `key_id: str, secret: bytes, status: str`. There is no public key field, no algorithm field, no fingerprint. Asymmetric signing means either a new dataclass or a discriminated union. This is the most invasive change.
- `sign_event()` (`_signing.py:43`) returns `(signature, canonical_hash, envelope)` where signature is raw HMAC bytes. BC-196's `alg` discriminator is not present. The signing function signature itself would need to change.
- `on_behalf_of.principal_id` is a bare string (`_contract.py:590`). No format, no attestation, no key binding. Any identifier format choice requires extending this validation, but it is just one function, not a cross-cutting change.

---

## 3. Position on Question A: Identifier format on the wire

**Answer: opaque ID + attestation-type enum.** With one refinement: the enum should be called `method` (not `type`) and should be a short string, not a closed enum, because new methods will arrive as deployment concerns.

### Why not raw key fingerprint

The prior round's "Agent positions" section (design doc line 414) argues for raw key fingerprint. This ignores what the code actually does:

1. `on_behalf_of.principal_id` at `_contract.py:590` is already a bare string with no format constraint. It could carry `"alice"` or `"sha256:abc123"` -- the code does not care. This is good. It means the protocol is already agnostic.
2. BC-196's acceptance criteria (BC-196 line 106) specify an `alg` discriminator on events. If the signing algorithm is pluggable (HMAC vs Ed25519 vs future), then the key fingerprint format also depends on the algorithm (SHA-256 of Ed25519 public key? BLAKE3 of P-256 compressed point?). Raw fingerprint couples the identifier to the signing algorithm. That is the wrong direction.

Raw fingerprint also kills the OIDC upgrade path. The design doc's own Q2 analysis (line 99) notes that OIDC subjects are the right shape for enterprise. If the wire format is `sha256:abc123`, you cannot later map that to an OIDC subject without a side registry -- which is exactly what `attestation_type: "oidc"` gives you for free.

### Why not DID

DID URIs add discovery and document-resolution semantics that substrate has no code for and no need for. The `validate_delegation_chain()` function at `_contract.py:582` does string checks. A DID resolver would be an entirely new dependency tree. The code cost is disproportionate.

### Code impact analysis

**For opaque ID + method:**

```
Changes required:
- _contract.py:582 validate_delegation_chain() -- extend principal_id
  validation to accept {id: str, method: str} or keep it as bare string
  (simplest option: keep principal_id as string, add optional
  principal_method field)
- _signing.py:16 build_signing_envelope() -- no change (on_behalf_of
  is already a dict; the method field is just another key in it)
- _keys.py:17 KeyEntry -- this changes regardless (BC-196), but the
  identifier format decision does not add to that change
```

**For raw fingerprint:** same `_contract.py` change (would need to validate fingerprint format), plus algorithm-specific fingerprinting baked into the signing envelope, making BC-196's algorithm-pluggability harder.

**For DID:** new dependency for DID resolution, changes to `_contract.py` (validate DID URI syntax), and the `on_behalf_of` dict would need to carry a DID document reference that nothing in substrate currently knows how to resolve.

**Opaque ID + method is the least invasive choice.** It keeps `principal_id` as a string (no change to `_signing.py`, no change to `_types.py`), adds one optional field that `validate_delegation_chain()` can check, and lets deployment decide what `method: "key"` means without substrate understanding Ed25519 vs P-256 fingerprints.

---

## 4. Position on Question B: Auditor access model

**Answer: Hybrid, but with a sharper boundary than "hybrid" usually implies.**

### What the workplace-audit use case actually requires

The driving scenario from `agent-provenance/README.md:7-8`:

> "Operators cannot reliably audit what actions the agent took, what files it touched, on whose authority, and at what time -- in a form an external auditor will accept."

The structural analog (`agent-provenance/README.md:25`):

> "The closest structural analog is AWS CloudTrail with log-file integrity validation."

CloudTrail's actual audit model (which SOX auditors accept) is:

1. **Live log in S3** with real-time write. Auditor gets read IAM role to the bucket. This is "direct verification."
2. **Digest files** delivered hourly. Each digest is signed by AWS and covers a time range. This is "durable audit bundles."
3. **Offline verification tool** (`aws cloudtrail validate-logs`) that checks digest chain + individual log file hashes. Works without any AWS API call.

CloudTrail uses both because real compliance audits have two phases:

- **Discovery** (weeks/months before the audit): auditor needs to sample the live log to scope the audit. They query for specific time ranges, specific users, specific API calls. This requires read access to the structured log. You cannot scope a SOX audit from a pile of signed JSON exports.
- **Evidence** (during the audit): auditor needs tamper-evident artifacts they can take away. The digest chain is the artifact. The auditor verifies it offline, on their own machine, months after the fact. This is the part that holds up under cross-examination.

### Why not direct-verification only

BC-198 (line 35-42 of the BC-198 breadcrumb) makes the case: "a tamper-evident log that the auditee can rewrite at will is not the property the auditor needs." Direct verification against the live log requires the auditor to trust that the log they are reading has not been tampered with since last time. An adversarial auditee can:

1. Show the auditor a clean log during discovery.
2. Modify events between the auditor's read and the formal evidence-collection phase.
3. The auditor's signature verification passes because the modified events are still correctly signed with the operator's key.

Without durable bundles (or witness signatures, or timestamp anchors -- BC-198 Layer 1), the auditor has no artifact that is independent of the auditee's current state.

### Why not bundles-only

A SOX audit requires the auditor to select the evidence, not receive it pre-selected. If the auditee prepares the bundle, the auditor must ask "why these events and not others?" Bundles work as the evidence artifact, not as the discovery mechanism.

### The specific hybrid

For v1 (solo + auditor), the model is:

1. **Log is signed plaintext** (substrate already does this via `_signing.py`).
2. **Auditor has read access** to the substrate event stream. They can query, sample, and scope their audit. This is direct verification.
3. **At audit close, the auditor (or automated tool) exports a signed bundle** of the relevant event range. The bundle is a Merkle subtree with a root hash signed by a timestamp authority (BC-198 Layer 1). This is the durable artifact.

The code path: substrate already stores `canonical_envelope` and `signature` per event (`_types.py:107`). The auditor's verification tool replays events, checks signatures against public keys, and computes a Merkle root. The bundle is just a serialization of the event range + Merkle proof + TSA token. No new substrate primitive needed -- just a tool that reads the event store and produces the artifact.

This maps to the "verifier" box in `agent-provenance/README.md:149` -- the offline verification tool. It already exists as a concept; the bundle is just a serialization format for its output.

---

## 5. Position on Question C: Recovery mechanism

**Answer: Second-device recovery key. Design the hook now; do not build it.**

### What constrains v1

The key v1 decisions that recovery must not break:

1. **`KeyEntry`** at `_keys.py:17` has no recovery-key concept. If v2 adds recovery, it needs either a `recovery_for: key_id` field on the `KeyEntry` or a separate recovery-key table.
2. **`on_behalf_of.principal_id`** at `_contract.py:590` is a string. If identity loss means new identity, the old `principal_id` becomes an orphan in the log -- all historical events are still verifiable but the actor is "dead." A recovery mechanism that preserves `principal_id` continuity requires a signed rotation event linking old key to new key.
3. **`verify_key_status()`** at `_keys.py:166` rejects revoked keys but has no concept of "superseded by recovery." The rotation flow would be: mark old key revoked, add new key with same `principal_id`, sign the rotation with the recovery key. This is an extension of the existing key-state machine, not a replacement.

### Why not the alternatives

**OIDC-only:** substrate has zero OIDC code today. No JWKS endpoint, no token validation, no IdP integration. Making recovery OIDC-only means OIDC is on the critical path for key rotation, which means OIDC must ship before recovery can work. That is a deployment constraint the single-person OSS posture rejects.

**Social recovery** (N-of-M peers): requires a multi-user trust graph that does not exist yet. The peer deployment model (Q3 consensus) means there is no shared infrastructure to coordinate peer signatures. Social recovery is a v3 feature at earliest -- it needs scenario 2 or 3 to be real first.

**No recovery, ever:** defensible for pure self-sovereign use, but the design doc's own Q7 consensus position (line 546-550) already describes a recovery-key mechanism: "at setup time, generate a second recovery key stored somewhere other than the primary key." The consensus already chose second-device; it just does not realize it.

### Why second-device recovery key

Second-device recovery requires one v1 schema addition: a `recovery_key_fingerprint` field in the key file format (the JSON file that `KeySet._load()` reads at `_keys.py:46`). The recovery key is an Ed25519 public key stored alongside the primary key entry. It can sign rotation events. The code change:

```
Key entry format (v1):
{
  "key_id": "primary-1",
  "status": "active",
  "secret": "<hmac-or-private-key>",
  "recovery_pubkey": "ed25519:base64..."   // <-- new, optional
}
```

The `KeySet` class does not need to understand recovery semantics -- it just needs to store the field so that the rotation tool can find it. The recovery flow (sign a rotation event with the recovery key, mark old key revoked, add new key) is v2 application logic that lives in agent-wake or a CLI tool, not in substrate.

### The v1 action item

Add `recovery_pubkey` as an optional field in the key file schema. Document that it is inert in v1. This is the only v1 change needed to keep recovery options open. It does not touch `_signing.py`, `_contract.py`, or `_types.py`.

---

## 6. Disagreements with consensus

### The Q2 contradiction between "Proposed positions" and "Agent positions"

The "Proposed positions" section (line 292) says:

> Opaque ID + side-channel attestation. The protocol should define only: `id` and `attestation_type: enum`.

The "Agent positions" section (line 414) says:

> The on-the-wire identifier should be a public key fingerprint... No DID overhead, no OIDC coupling.

These are different answers. The proposed positions want `{id, type}`; the agent positions want bare key fingerprint. The "Reader input" section (line 609) lands on `{id, key_fp, binding}` -- a third option.

The right answer is the proposed positions' `{id, method}` (argued in Section 3 above). But the document should acknowledge that three positions were staked and two of them disagree. Right now it reads as if there is consensus.

### Q6: Auditors should have the identity primitive

The consensus (line 508) says "auditors don't need the identity primitive." Then it immediately describes auditor attestations -- signed events in the log -- that use the identity primitive. An auditor who signs an attestation event needs a key, needs a `principal_id`, and needs that identity to be recognizable in the log. That is the identity primitive.

The distinction the consensus tries to draw ("verifier identity, not actor identity") is false. The code does not distinguish: `_signing.py:sign_event()` takes `actor_id` and `on_behalf_of`. An auditor's attestation event goes through the same path. The auditor is an actor. The code already treats them as one. Do not create a separate concept that the code does not support.

---

## 7. Things the design doc misses entirely

### Missing question: Timestamping and ordering authority

The design doc discusses identity, delegation, and key management. It does not discuss who provides the clock. BC-198 (operator forgery defense) is mentioned in passing but not treated as a design question. It should be.

The `timestamp` field on events (`_types.py:102`) is set by the client. An adversarial operator can backdate it. BC-198 Layer 1 (RFC 3161) mitigates this, but:

- Who runs the TSA? For SOX, the auditor may require a TSA they trust, not one the auditee picked.
- What is the batch interval? Hourly? Per-event? This is a config question that affects audit defensibility.
- The `event_seq` integer (`_types.py:95`) provides ordering within a single substrate instance but not across instances (scenario 3, cross-org). Cross-instance ordering needs an external anchor.

This is a Q10-sized gap. It should be a numbered question.

### Missing question: Identity migration and namespace stability

The design doc assumes `principal_id` is stable. But key rotation (Q7 consensus) means the same human can have different `principal_id` values over time (if `principal_id` is a key fingerprint). Even with opaque ID + method, if the key rotates, does the ID stay the same?

The `on_behalf_of.principal_id` at `_contract.py:590` is a bare string. Historical events carry the old value. The auditor needs to know that `principal_id: "alice-key-v1"` and `principal_id: "alice-key-v2"` are the same human. This mapping needs to exist somewhere -- in the log (rotation events), in a registry, or in the ID format itself (DIDs handle this, which is the one argument for DIDs that the design doc does not engage with honestly).

The design doc should have a question about identity continuity across key rotation. It does not.

### Missing question: What happens when substrate is not present

The consensus on Q1 says identity lives in substrate. Q3 says peer model with optional shared DB. But the proposed positions also say (line 285) "a lightweight deployment wants to run without substrate." What signs events in that case? What provides the delegation chain?

`agent-wake/AGENTS.md` (line 288) already notes that HTTP-only ingest should work without substrate. If identity lives in substrate but substrate might not be present, there is a gap. The design doc should address the substrate-less path explicitly: is it "bring your own signing" or "unsigned mode"?

### Code-level gap: `KeyEntry.secret` is bytes, not algorithm-tagged

`_keys.py:19` stores `secret: bytes`. For HMAC, this is the shared secret. For Ed25519, this would need to be a private key -- but the same field cannot distinguish between "these are HMAC key bytes" and "these are an Ed25519 private key." BC-196's `alg` discriminator needs to land at the `KeyEntry` level, not just at the event level. The design doc does not call out that `_keys.py` needs the most invasive change of any file in substrate for multi-user to work.

---

## Summary

| Question | Position |
|----------|----------|
| A: Wire identifier | Opaque ID + method string. Least invasive to `_contract.py`, no change to `_signing.py` or `_types.py`. Preserves OIDC and algorithm-pluggability upgrade paths. |
| B: Auditor access | Hybrid. Direct verification for discovery; durable Merkle bundles + TSA for evidence. Maps to existing verifier tool concept in `agent-provenance/README.md`. |
| C: Recovery | Second-device recovery key. Add `recovery_pubkey` to key file schema as optional field in v1. No substrate code path changes needed. |
