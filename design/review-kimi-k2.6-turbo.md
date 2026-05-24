# Review: Identity and multi-user design questions
**Reviewer:** Kimi-K2.6-turbo  
**Date:** 2026-05-23  
**Source prompt:** `/projects/agent-wake/design/identity-and-multi-user.md` — three remaining disagreements (A, B, C).

---

## 1. Pre-reading attestation

| File read | Most surprising finding |
|-----------|------------------------|
| `/projects/agent-wake/design/identity-and-multi-user.md` | The "Agent positions" section on Q2 (identity primitive) flatly contradicts the consensus "Proposed positions" Q2 — the agent argues for raw key fingerprints while the draft consensus says opaque ID + attestation-type enum. This is not noted as a live disagreement in the doc, but it is one. |
| `/projects/substrate/src/substrate/_signing.py` | `verify_event` already contains a backward-compatibility fallback for old envelopes that lack `on_behalf_of` (lines 93–98). That shows the authors anticipated schema evolution in the signing surface — a good sign for the "least invasive change" criterion. |
| `/projects/substrate/src/substrate/_keys.py` | `KeyEntry` is a three-field dataclass (`key_id`, `secret`, `status`) and `verify_key_status()` already enforces the `revoked` state. The surprise is how *close* the existing key lifecycle is to what's needed for rotation, and how far it is from what's needed for asymmetric verification — there is literally no `public_key` field. |
| `/projects/substrate/breadcrumbs/resolved/196-hmac-symmetric-no-external-verifiability.md` | BC-196 explicitly recommends Ed25519 as the minimum asymmetric scheme and proposes an `alg` discriminator on events. It does *not* propose DIDs. The breadcrumb treats DID-level complexity as out of scope for the minimum credible offering. |
| `/projects/substrate/breadcrumbs/resolved/197-no-delegation-chain-agent-actor-cannot-bind-to-human.md` | BC-197 is marked **implemented**, but its acceptance criteria still have unchecked boxes for "per-principal signing keys" and "external auth-provider integration." The implementation was the typed `on_behalf_of` sub-object; the cryptographic enforcement is explicitly deferred to BC-196. This means the "implemented" label is structural, not cryptographic. |
| `/projects/agent-provenance/README.md` | §6 architecture sketch already draws the verifier as an **offline tool** that replays exported substrate events and emits an "auditor-ready report." This is strong, under-used evidence for the durable-bundle side of Question B. |

---

## 2. Substrate code findings

### What substrate already supports (that the doc treats as future work)

- **`_signing.py` `on_behalf_of` parameter.** The envelope builder accepts a `dict | None` and includes it in canonical JSON. BC-197 calls this the "cheap, forward-compatible" v1 fix. It is already shipped.
- **`_signing.py` backward-compat retry in `verify_event`.** When `on_behalf_of` is absent, the verifier falls back to an old envelope shape. This proves the signing layer can evolve without invalidating prior events.
- **`_keys.py` key lifecycle states.** `active`, `deprecated`, `revoked` are already validated in `_load()` (line 73) and `verify_key_status()` (lines 166–175). Rotation and revocation are partially operational today.
- **`_keys.py` hot-reload.** The `KeySet` polls mtime and reloads. Any registry-based identity resolution can reuse this mechanism.

### What substrate does *not* support (that the doc assumes will exist)

- **Asymmetric verification material.** `KeyEntry` has `secret: bytes` and no `public_key`. BC-196 cannot be implemented without adding at least one new field to `KeyEntry` and a new verification path in `_signing.py`.
- **Timestamped revocation.** `verify_key_status()` rejects a `revoked` key unconditionally. There is no `revoked_at` timestamp, so verifiers cannot distinguish "signed before revocation" from "signed after." The opencode agent reader already flagged this gap.
- **`alg` discriminator on events.** BC-196 proposes this so verifiers can dispatch between HMAC and Ed25519. `_signing.py` `sign_event` and `verify_event` have no `alg` parameter.
- **Per-principal signing.** `sign_event` takes a single `key: bytes`. Multi-signature or co-signing (needed for social recovery or multi-party delegation) would require a new signature envelope shape.
- **DID resolution.** There is no network client, no DID document parser, and no caching layer in substrate. A DID URI candidate would require building all of this from scratch.

---

## 3. Position on Question A: Identifier format on the wire

**Answer: Opaque ID + attestation-type enum.**

### Why, grounded in substrate code

**Raw key fingerprint**  
- `_signing.py`: `actor_id` is already a `str`; it can hold a fingerprint with zero envelope changes.  
- `_keys.py`: The `key_id` field is already a `str`. However, the fingerprint *is* the key identifier, so every key rotation changes the `actor_id`. The `KeySet` class currently maps one `key_id` → one `KeyEntry`. It has **no linking table** to say "`key_id_new` supersedes `key_id_old`." Adding rotation chains means either (a) changing the `KeySet` JSON schema to include `supersedes` / `previous_key_id` fields, or (b) forcing every deployment to update all actor configurations whenever a key rotates.  
- Verdict: Low envelope cost, high lifecycle cost. Fails the "least invasive" test because rotation is not a first-class concept in `_keys.py` today.

**DID URI**  
- `_signing.py`: `actor_id` could hold the URI, but the verifier would need to resolve it.  
- `_keys.py`: Would need a DID resolver, HTTP client, document parser, and method-specific handlers. The class currently loads keys from a local JSON file via `_load()`; adding DID resolution is a scope expansion that violates the "single-person OSS" posture in `agent-provenance/README.md` §8.  
- Verdict: Massively invasive. BC-196 explicitly avoids this for the "minimum credible offering."

**Opaque ID + attestation-type enum**  
- `_signing.py`: The envelope gains at most one new field (or a key inside the existing `on_behalf_of` dict). `build_signing_envelope()` already takes `on_behalf_of: dict | None`; adding `"attestation_type": "key"` to that dict is a one-line change that reuses the existing schema evolution path.  
- `_keys.py`: The `key_id` stays as the opaque identifier. The `KeySet` continues to resolve by `key_id`. The *actual* verification material (public key, JWKS URL, DID document) lives in a deployment-specific registry outside substrate's core. Substrate only needs to know "how do I verify a signature given a `key_id` and an `alg`?" — which is exactly what BC-196 already requires. The attestation-type enum is a **protocol-layer** hint, not a substrate-layer resolver.  
- Rotation: The opaque ID stays constant; only the registry entry changes. No retrofit to `_keys.py` rotation logic is required.
- Verdict: Least invasive to substrate, fully compatible with BC-196's `alg` field, and preserves the auditor model because the auditor resolves the ID through the same deployment registry.

### The auditor-model check

The auditor model is defined in `agent-provenance/README.md` §1: "verifiable offline by a third-party auditor." Under the opaque-ID option, the auditor receives:
1. The event envelope (opaque `actor_id`, `attestation_type`).
2. A copy of the deployment registry (static JSON, JWKS, or DID document).
3. The public key corresponding to that registry entry.

The auditor verifies without touching the live system and without substrate needing to know what a DID or OIDC is. This satisfies the requirement.

---

## 4. Position on Question B: Auditor access model

**Answer: Hybrid, with durable audit bundles as the primary compliance mechanism and direct verification as a convenience for solo+auditor dogfood.**

### Grounding in the actual use case

The driving scenario is the workplace audit regime described in `agent-provenance/README.md` §8: "Build to the owner's compliance team's actual standards... They are the rare gift of a real auditor available for questions."

The README names the compliance analog explicitly: **AWS CloudTrail with log-file integrity validation** (§1). CloudTrail does not give auditors a SQL login to the live control plane. It delivers **S3-hosted, integrity-validated JSON log files** that auditors inspect offline. This is not incidental; it is the structural standard that external auditors (SOX, HIPAA, SOC 2 Type II) are trained to consume.

What real audits require:

| Regime | What auditors demand | Why direct verification alone fails |
|--------|---------------------|-----------------------------------|
| **SOX ITGC** | Point-in-time evidence of access controls; auditors test controls as of a specific date. | Live access lets the auditee modify logs between the test date and the review date. |
| **HIPAA §164.312(b)** | Audit controls and access logs; OCR investigations request **exported** records. | Direct DB access creates a data-egress risk and is rarely granted to external assessors. |
| **SOC 2 Type II** | Auditor reviews a **sample** of historical events over the observation period. They work from exports, not live queries. | A live system can be patched, rotated, or reconfigured between sampling and reporting. |
| **Internal IT audit** | Monthly or quarterly log extracts for trend analysis. | Direct verification is feasible here, but the extracts are still the durable artifact. |

### Why durable bundles are load-bearing

The README's architecture sketch (§6) already draws the verifier as an **offline tool** that replays substrate events and emits an "auditor-ready report." This implies the substrate log is exported before verification. The design doc's "Proposed positions" on Q6 say auditor attestations should be "signed events in the log" — but an auditor cannot sign an attestation *in* the live log unless they have write access, which contradicts the read-only auditor role. The cleaner model is: the auditor signs the **bundle** (or a Merkle root of the bundle), and that signature is a first-class artifact *outside* the live log.

BC-198 (operator-forgery defense) also points toward bundles: it proposes "periodic Merkle-root co-signatures by N independent parties." A Merkle root is computed over a **segment** of the log — i.e., a bundle. You cannot compute a periodic Merkle root over a live, append-only stream without defining segment boundaries.

### Why hybrid is the right framing

- **Durable bundles** satisfy the arms-length compliance audit (the actual workplace use case). The bundle is signed at export time; the auditor verifies it offline; the operator cannot retroactively modify the segment without invalidating the export signature.
- **Direct verification** is a degenerate-case convenience for solo+auditor dogfood. It is not sufficient for external regulatory audit because the live system remains under operator control.
- The hybrid label matters because the design doc must not *prohibit* direct read access for debugging and monitoring, but the **protocol guarantee** must be bundle-based.

### Citation from README

> "**CloudTrail for agent actions, vendor-independent, verifiable offline by a third-party auditor.**" (`agent-provenance/README.md` §1)

If the project literally wants to be "CloudTrail for agent actions," the auditor model must be "log files delivered to S3," not "auditor logs into your Postgres."

---

## 5. Position on Question C: Recovery mechanism (v2)

**Answer: Second-device recovery key.**

### Constraint: do not preclude v1 decisions

V1 substrate primitives that recovery must compose with:
- `_signing.py` `sign_event` takes exactly one `key: bytes` and returns one signature.
- `_signing.py` `verify_event` verifies one signature against one key.
- `_keys.py` `KeyEntry` has `key_id`, `secret`, `status`.
- `_keys.py` `KeySet` loads from a single JSON file with a `"keys"` array.
- BC-197's `on_behalf_of` dict is the delegation primitive.

### Retrofit cost per option

**Second-device recovery key**  
- `_keys.py`: Add an optional `role` field to the JSON schema (e.g., `role: "primary" | "recovery"`). The `_load()` method already validates `status` on line 73; adding `role` validation is a ~3-line change. The recovery key is stored in the same `KeySet` file or a secondary file read by the same class.  
- `_signing.py`: A key rotation signed by the recovery key uses the **existing** `sign_event` with the recovery key's `secret` bytes. The `on_behalf_of` dict already exists; it can carry a `delegation_type: "recovery_rotation"` field to explain why the primary key isn't signing.  
- Verdict: **Low retrofit.** No multi-sig, no new signature envelope, no network calls.

**Social recovery (N-of-M)**  
- `_signing.py`: `sign_event` would need to accept `keys: list[bytes]` and produce `signatures: list[bytes]`. The return type is currently `tuple[bytes, bytes, bytes]` (signature, hash, envelope). Changing this to support multiple signatures is a **breaking schema change** to every event. `verify_event` would need threshold logic.  
- `_keys.py`: Would need to understand quorum rules and peer key sets.  
- Verdict: **High retrofit.** Precludes v1's single-signature envelope.

**OIDC-only**  
- `_keys.py`: The `KeyEntry` `secret` field assumes a symmetric HMAC secret or an asymmetric private key. An OIDC-only model means the `secret` is not signing material at all — it's an OIDC client secret or absent. This forces substrate to bifurcate "keys" into "signing keys" and "identity bindings," which is a medium-sized schema change.  
- It also couples v1 to an external IdP, which the README explicitly defers to v2 (`agent-provenance/README.md` §9: "Real IdP integration (OIDC, SAML) is a v2 question").  
- Verdict: **Medium retrofit, and violates v1's self-sovereign default.**

**No recovery, ever**  
- Zero code changes, but it makes the "workplace audit" use case brittle. If the human principal loses their laptop (and key), the compliance chain breaks. This is acceptable for a hobbyist; it is not acceptable for a workplace where the compliance team (§8) must explain key-loss incidents to an external auditor.  
- Verdict: **Zero retrofit, but precludes enterprise credibility.**

### Why second-device is the right v2 path

It is the only option that:
1. Fits inside the existing `KeySet` JSON schema with a minor addition.
2. Uses the existing `sign_event` / `verify_event` single-signature path.
3. Uses the existing `on_behalf_of` dict for semantic annotation.
4. Does not preclude OIDC-only later (the attestation-type enum from Question A can add `type: "oidc"` in parallel).
5. Matches real-world practice: hardware wallets, PGP backup keys, and AWS IAM "cross-account" recovery all use a second, pre-registered credential.

Recommended v1 preparatory step: add the `role` field to `KeyEntry` and document that `"recovery"` keys are valid *only* for `key_rotation` events. This is a ~5-line change in `_keys.py` and makes v2 recovery a policy addition, not a schema retrofit.

---

## 6. Disagreements with consensus (optional)

### Q6: Auditors as a first-class role

The "Proposed positions" say: "No special 'auditor' role type. An auditor is simply another identity that has been granted read access to a provenance stream."  
The "Agent positions" dissent more sharply: "Auditors don't need the identity primitive. They need public keys and a verification API."

I agree with the **Agent positions** on this point, not the consensus. The consensus overloads the actor-identity primitive (which has key rotation, delegation chains, and sender-gating policies) with an access-control concern. Auditors do not sign tool calls, do not need delegation chains, and do not need sender gating. Giving them a "role" in the same registry invites the mistake of treating read access as a cryptographic identity problem.

The better model, which the Agent positions sketch and I endorse, is:
- **Actor identity** = signing key for events in the log.
- **Auditor access** = read token or public-key allowlist, managed outside substrate's core identity registry.
- **Auditor attestation** = signed by the auditor's own key, but stored as a separate artifact (a bundle co-signature, not an event in the live log).

This keeps substrate's identity primitive narrow, which is consistent with the "substrate should not become an identity provider" caveat in the Reader input.

### Q7: Recovery in v1

The consensus says: "Recovery: out of scope for self-sovereign mode. If you lose your key, you create a new identity."  
I do not object to this as a v1 shipping boundary, but I object to the framing that recovery is therefore a v2 *surprise*. The v1 schema should leave a mechanical hook for recovery so that v2 does not require a migration. Adding the `role` field to `KeyEntry` (see Question C above) is that hook. It is cheap and should be in the v1 key-management PR.

---

## 7. Things the design doc misses entirely

### 7.1 Agent-key vs. human-key topology (should be Question 10)

The doc asks "who is the actor?" and "who is the principal?" but never asks **whether the agent harness should hold its own signing key distinct from the human's key.**

This matters cryptographically. Today, substrate signs with a single key. In the agent-provenance model, the *agent* executes tool calls and the *human* authorizes them. If they share a signing key:
- A compromised agent can sign events that look like they came from the human.
- Revoking the agent means revoking the human's identity.
- The delegation chain (`on_behalf_of`) is structurally present but cryptographically meaningless because both links are signed by the same key.

The right topology is:
- Human holds an identity key (long-lived, in `~/.config/`).
- Agent holds an ephemeral session key, generated per-session or per-harness-start.
- Human signs a **session grant** (delegation certificate) that says "public key `0xAgent...` may sign events on my behalf for the next 8 hours."
- Agent signs events with its session key; verifier checks the event signature against the agent key, then checks the session grant signature against the human key.

This is the natural cryptographic expression of BC-197's delegation chain, and it is blocked today because `_signing.py` only accepts one `key: bytes`. The design doc should ask: **"Does the agent harness get its own ephemeral keypair?"** The answer has massive consequences for key management, revocation, and prompt-injection resistance.

### 7.2 Revocation latency and the "window of suspicion"

The doc discusses revocation but assumes compromise is detected instantly. In practice, you detect compromise at time `T_detect`, but the key was compromised since `T_breach`. `_keys.py` `verify_key_status()` rejects a revoked key unconditionally; it has no `revoked_at` or `compromised_at` timestamp. Without bounding the window `[T_breach, T_detect]`, an auditor cannot reason about which events are suspect.

The missing question: **"What is the maximum acceptable revocation latency, and how does the verifier handle events signed during the suspected compromise window?"** This is a real audit question (SOC 2 log review, incident response) that the current `active | deprecated | revoked` trinary cannot answer.

### 7.3 GDPR / data-retention collision with append-only logs

An append-only event log that contains filenames, tool arguments, or user identities is a GDPR personal-data processing system. The right to erasure (Article 17) conflicts with cryptographic immutability. The design doc never asks: **"How do we handle deletion requests without breaking the signature chain?"** One possible answer is "redaction labels" (hash the sensitive field, store the ciphertext elsewhere), but the question should be asked before v1 schema freezes.

---

*End of review.*
