# Identity and multi-user — review

**Reviewer model:** deepseek-v4-pro
**Date:** 2026-05-23

## 1. Pre-reading attestation

| File | Most surprising finding |
|---|---|
| `design/identity-and-multi-user.md` | The opencode reviewer's section (line 584-801) already identified the `on_behalf_of` field and key lifecycle states as existing infrastructure, and proposed a fourth identifier format (`{id, key_fp, binding}`) not listed among the three Question A candidates |
| `substrate/_signing.py` | `build_signing_envelope()` (line 10-26) already has `on_behalf_of: dict | None` as a first-class parameter, and `verify_event()` (line 93-98) has a backward-compat path for old events without it — delegation is already wired into the signing surface |
| `substrate/_keys.py` | `verify_key_status()` (line 166-175) distinguishes `revoked` (raises) from `deprecated` (warns) — revocation is a first-class concept, not future work |
| `breadcrumbs/196` | Acceptance criteria (line 103-108) require an `alg` discriminator on event payloads — any identifier format must coexist with this field |
| `breadcrumbs/197` | Status is `implemented`, not `accepted` — the `on_behalf_of` sub-object (`principal_id`, `session_id`, `authenticated_at`, `scope`) is richer than the design doc's flat `trigger_identity`/`actor_identity` pair |
| `agent-provenance/README.md` | §2 (line 46-51) requires the scope statement itself to be a signed first-class event — a meta-attestation none of the nine design questions address |
| `breadcrumbs/198` | Layer 1 (RFC 3161 timestamps) is "implementable today against the existing HMAC primitive" (line 189-190) — temporal integrity doesn't wait for BC-196 |

## 2. Substrate code findings

### What substrate already supports that the design doc treats as future work

- **`build_signing_envelope()` in `_signing.py:10-26`** already accepts `on_behalf_of: dict | None`. This is BC-197's minimum-viable fix, shipped. The design doc (line 322-330) proposes a tamper-evident envelope with dual identities; substrate's envelope already has a slot for exactly that.

- **`KeyEntry` in `_keys.py:17-20`** has `status` with `active`/`deprecated`/`revoked` states. `verify_key_status()` at `_keys.py:166-175` dispatches on these. The design doc (line 358-364) describes rotation and revocation as proposals; substrate already implements them.

- **`key_id` is a free-form string.** `_keys.py:71` reads `key_id` from the JSON config; `get_key(key_id)` at `_keys.py:135` looks it up. There is no constraint that `key_id` be a fingerprint — it can be any opaque string. This means the wire identifier format can evolve without touching the key lookup path.

### What substrate does not support that the design doc assumes

- **Asymmetric signing (BC-196).** `compute_hmac()` at `_signing.py:29` is the sole signature primitive. The design doc's auditor verification model (position #6, line 348-354) assumes auditors can verify signatures with a public key — under HMAC they'd need the shared secret, which breaks the adversarial auditor threat model (BC-196 lines 23-33).

- **The `alg` discriminator.** BC-196 acceptance criteria (line 108) require one. No code exists. The design doc's "vendorable wire schema" (position #1, line 276-288) doesn't address how consumers handle unknown `alg` values.

- **Key generation.** `_keys.py:_load()` reads keys from a file; nothing generates them. The design doc's "local CLI command" (position #7, line 358) would need to produce a key-entry JSON blob that `_keys.py` can consume. The `KeyEntry` dataclass at `_keys.py:17-20` has `secret: bytes` — for Ed25519 this field would carry the full private key bytes. No structural change needed; just a new producer.

- **Timestamp anchoring (BC-198).** No code exists. The auditor model needs this for temporal integrity but it's orthogonal to identity.

## 3. Position on Question A: opaque ID + attestation-type enum

**Answer: candidate 2** (`{id, type: "key" | "oidc" | "did"}`).

**Candidate 1 (raw key fingerprint) appears minimal but isn't.** `build_signing_envelope()` at `_signing.py:13` already takes `actor_id: str` — any string works. But BC-196 (line 103-108) requires an `alg` field on every event payload regardless. A raw fingerprint without algorithm metadata leaves the verifier guessing — is `sha256:abc123` an Ed25519 key or an HMAC secret? You'd need a separate algorithm lookup table. So candidate 1 either (a) doesn't save you the extra field anyway (you need `alg` from BC-196) or (b) forces you to encode the algorithm into the fingerprint format (e.g., `ed25519:abc123`), which is just an ad-hoc type enum with worse naming.

**Candidate 3 (DID URI) breaks the existing key resolution path.** `_keys.py:get_key()` at line 135 does a local dict lookup: `self._keys.get(key_id)`. DIDs require DNS lookup or document resolution — a network call. This is a fundamentally different `KeySet` architecture. Additionally, DIDs have their own rotation/discovery story that conflicts with substrate's signed-event rotation model (`_keys.py`'s `active`/`deprecated`/`revoked` states). You'd have two rotation mechanisms that can disagree.

**Candidate 2 fits the existing architecture with one field addition:**

```
# _keys.py line 17-20 — current
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
```

Add `attestation_type: str = "key"`. That's the only code change in substrate's core. The `actor_id` in the signing envelope stays a free-form string. The `attestation_type` lives in the key registry, not on the wire. Verifiers dispatch verification logic based on it. A new attestation type adds a value to the enum without touching `_signing.py` at all.

This also matches the existing house style: `on_behalf_of` at `_signing.py:16` is `dict | None` — structured but optional metadata on top of a plain identifier. Candidate 2 applies the same pattern to identity.

**Note on the opencode reviewer's fourth option:** The `{id, key_fp, binding}` shape (line 613-619) over-parameterizes. `key_fp` is just the `id` when `type == "key"`. Adding it as a required field forces all attestation types to carry a fingerprint, which OIDC-backed identities don't need. The simpler `{id, type}` leaves the binding to the deployment.

## 4. Position on Question B: hybrid (direct for dogfood, bundles for compliance)

The workplace-audit use case in `agent-provenance/README.md` implies a specific audit regime:

- §8 (line 196-208): "Build to the owner's compliance team's actual standards" — this is a real compliance team with real standards, not an imagined one. The README calls them "the rare gift of a real auditor available for questions."
- §1 (line 11-16): The target is "a form an external auditor will accept."
- §2 (line 33-35): "Offline verification by a third-party auditor" is an explicit requirement.

For the compliance regimes relevant to a workplace deploying AI agents:

**SOX §404**: The external auditor tests management's internal controls. They need independent evidence — they cannot rely solely on the auditee's representations or systems. A live log that the operator can rewrite (BC-198 lines 30-47: "an entity that holds the private key can sign anything it wants") fails this test. The auditor needs evidence they hold, not evidence they query. Bundles satisfy this; direct verification does not.

**HIPAA §164.312(b)**: Requires audit controls that "record and examine activity in information systems." HIPAA audits are document-review: the auditor requests records, and the covered entity produces them. The auditor does not get live system access as a matter of course. Bundles match this workflow; direct verification doesn't.

**Internal IT audit (IIA standards)**: Requires independence. The auditor should not rely on the auditee's system for evidence integrity. If the same person runs the agent harness and the substrate instance, "just check the live log" is circular — the auditee controls the evidence source. Bundles with RFC 3161 timestamps (BC-198 Layer 1, implementable today per line 189-190) give the auditor independent temporal evidence.

**Position: hybrid, with a concrete boundary.**

- Direct verification is for development, dogfooding, and the "solo + auditors" scenario where auditor and operator are the same person or mutually trusting. This is the homelab threat model from BC-196 line 22-24 and BC-198 line 51-52.
- Durable audit bundles are the compliance path. The bundle is a signed JSON export of a log segment, with RFC 3161 timestamps attached. The auditor receives the bundle through a channel independent of the operator's live system (email, shared drive, USB). They verify signatures offline using public keys obtained at setup time through a separate channel. They can then sign an attestation event ("I reviewed this segment") — satisfying settled position #6 (line 348-354).

The hybrid isn't hand-waving. It's the natural consequence of BC-198's layered trust model: Layer 0 is direct (homelab), Layer 1 is bundles + timestamps (compliance). Both use the same code paths; the difference is whether the consumer queries live or reads an export.

## 5. Position on Question C: second-device recovery key

**Answer: candidate 1 (second-device recovery key), designed now as v1 schema, implemented in v2.**

Traced against v1 code:

**Candidate 1 constrains v1 the least.** The only change is to `KeyEntry` at `_keys.py:17-20`:

```
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
    kind: str = "primary"   # NEW: "primary" | "recovery"
```

The recovery key is just another key with different `kind`. Rotation is already handled by `_keys.py`'s active/deprecated states. The signing envelope at `_signing.py:10-26` already supports multiple keys via `key_id`. A recovery-signed rotation event is structurally identical to any other rotation event — it goes through `sign_event()` at `_signing.py:43`, gets an HMAC (later Ed25519) signature, and enters the log. Zero envelope changes. One field on a dataclass.

**Candidate 2 (social recovery)** requires multi-signature verification. `verify_event()` at `_signing.py:72` verifies exactly one signature against one key. Multi-sig means `verify_event` needs to accept and check N signatures against N keys. This is BC-196's co-signature layer (BC-196 line 84-89), which is deferred past v1. It also requires peer discovery infrastructure that the peer deployment model (settled position #3) doesn't provide. Substantial retrofit.

**Candidate 3 (OIDC-only)** requires an IdP integration module. BC-197 remedy #4 (line 133-137) says this "is a real integration project and belongs after the v1 demo." It also abandons self-sovereign users, contradicting position #2's commitment to key-based identity as the base. Moderate retrofit, wrong posture.

**Candidate 4 (no recovery)** requires zero v1 changes but weakens the auditor model. If an auditor asks "what happens when the operator loses their key?" and the answer is "all provenance is lost, start over," the auditor may reject the system as a control — because the system can't prove continuity of identity across key compromise. A designed recovery path (even if not yet implemented) closes this objection.

The `kind` field on `KeyEntry` costs nothing to add. Recovery key generation at setup time costs nothing to document. The implementation — using the recovery key to sign a rotation event — uses the existing `sign_event()` path unchanged. It doesn't block anything; it doesn't require BC-196; it precludes none of the other candidates (you can still add social recovery later, and the recovery key mechanism still exists as a fallback). And it makes the v1 documentation honest: "v1 doesn't implement recovery, but the schema supports it, and here's how it will work."

## 6. Disagreements with consensus

**Settled position #7 (no recovery magic, line 356-371) overcorrects.** The opencode reviewer's Q7 (line 527-549) proposed a recovery key mechanism and was folded into the "agent positions" section — but the "proposed positions" section above it (line 356-371) says "out of scope for self-sovereign mode." These two contradict. A recovery key is not "recovery magic" — it's a second key with a different operational role, structurally identical to the primary key, stored in a different location. `_keys.py` already handles multiple keys with different statuses. The consensus position dismisses it as complexity; it's actually the simplest thing that doesn't preclude recovery later.

**Settled position #6 (auditors are identities too, line 348-354) conflates roles.** An auditor who signs attestation events uses the same identity primitive as an actor who signs tool-call events. But the threat models differ: an actor's key compromise means forged actions (bad); an auditor's key compromise means forged attestations (worse — it corrupts the verification layer itself). The auditor probably needs stronger key management (hardware-backed? shorter rotation cycle?) but the unified identity primitive doesn't distinguish these needs. The opencode reviewer's Q6 (line 509-519) correctly identifies this tension: "the auditor's key is not an 'actor identity' — it's a verifier identity, and it doesn't need the same infrastructure." I'd add a `role` field to `KeyEntry` rather than a separate primitive, but the distinction matters.

## 7. Things the design doc misses entirely

### 7.1 The scope attestation is a first-class identity question

`agent-provenance/README.md` §2 (line 46-51) says the scope statement — "this substrate instance captures events from configured harnesses A, B, C" — must be a signed first-class event. Who signs it? Under what identity? Can it be updated (new harness added) without breaking the audit chain? An auditor who verifies every event's signature but can't verify what the system was supposed to capture is verifying integrity without completeness. BC-198 (line 150-155) names this as an explicit non-goal for substrate — it's the consumer's problem. But the design doc's nine questions don't address it.

### 7.2 Key distribution bootstrap problem

The peer model (settled position #3) says users exchange public keys out-of-band. But how does an auditor obtain the actor's public key? If the auditor receives a signed bundle and asks the operator for the public key to verify it, the operator could provide a fake public key and sign with the corresponding private key. The auditor needs the public key through a channel the operator doesn't control. `_keys.py` currently loads keys from a local file — there's no export mechanism, no key distribution protocol, and no discussion of this bootstrap problem anywhere in the design doc.

### 7.3 Forward compatibility for the `alg` field

BC-196 (line 103-108) requires an `alg` discriminator on event payloads. Once events carry this, every consumer must handle unknown `alg` values. If `agent-provenance` receives an event with `alg: "Ed25519"` but it only understands HMAC verification, what does it do? Reject? Skip verification? Log a warning and continue? The design doc's "vendorable wire schema" (position #1, line 276-288) needs a forward-compatibility contract. The `build_signing_envelope()` at `_signing.py:10-26` doesn't currently include `alg` — when it's added, all consumers that parse the envelope must handle it.

### 7.4 Temporal integrity vs. identity claims

BC-198's RFC 3161 timestamps prove data existed at a specific UTC time. If an event's `authenticated_at` field (from BC-197's `on_behalf_of` sub-object) claims the human authenticated at 14:32 UTC, but the RFC 3161 timestamp on that event's batch says the batch was submitted at 14:45 UTC, what does the verifier conclude? The two timestamps are consistent but not identical — is there a tolerance window? No question in the design doc addresses the relationship between application-layer timestamps and cryptographic timestamps.
