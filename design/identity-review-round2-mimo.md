# Identity and multi-user review — Round 2

**Reviewer model:** MiMo-V2.5-Pro (xiaomi-token-plan-sgp/mimo-v2.5-pro)
**Date:** 2026-05-23
**Status:** Submitted for synthesis

---

## 1. Pre-reading attestation

| File | Most surprising finding |
|---|---|
| `identity-and-multi-user.md` | The "opencode agent" reader input at line 613 proposes `{id, key_fp, binding}` — three fields — while the "Proposed positions" at line 292 proposes only `{id, attestation_type}`. These are different schemas and the doc doesn't reconcile them. |
| `_signing.py` | `build_signing_envelope` (line 10-26) covers `actor_id` and `on_behalf_of` but **not** `key_id` or `timestamp`. The key that signed an event is not in the signed payload. The timestamp is not in the signed payload. Both are stored on the `Event` object but are not integrity-protected by the signature. |
| `_keys.py` | `KeyEntry` (line 17-20) has `key_id: str` — an opaque label, not a fingerprint. There is no `fingerprint()` method. The key file schema has no field for public key material; it stores only `secret` (bytes). This means the system is HMAC-only at the data-model level. |
| `agent-provenance/README.md` | The trust model (line 81-97) explicitly stages asymmetric signing as layer 4, **after** the delegation chain (layer 2) and RFC 3161 timestamps (layer 3). This ordering matters: the delegation chain is schema-visible but not cryptographically enforceable until asymmetric signing lands. |
| BC-197 breadcrumb | Status is `implemented` (line 21). The `on_behalf_of` field with `validate_delegation_chain()` is already in production code. But the breadcrumb's own text (line 168) says "cryptographic enforcement is BC-196 + future work." So the delegation chain is there but is still self-attested — any holder of the HMAC key can forge it. |
| BC-196 breadcrumb | Status is `accepted` (line 8), not `implemented`. The "pluggable signature scheme" is remedy (1) — the structural prerequisite for everything else — and it is not built. This is the blocking dependency for the entire multi-user design. |

## 2. Substrate code findings

**Already supported (design doc treats as future work):**

- `on_behalf_of` delegation chain — implemented across the full stack: `_signing.py:16`, `_event_store.py:80`, `_contract.py:582-613` (`validate_delegation_chain`). The validation requires `principal_id` (string), optional `scope` (list of strings), optional `authenticated_at` (string). This is BC-197, and it's done.
- Key lifecycle states — `_keys.py:73` validates `active | deprecated | revoked`. `verify_key_status()` at line 166 rejects revoked keys and warns on deprecated keys. This is already the skeleton for key rotation.
- Hot-reload of key material — `_keys.py:122-133`. The key file is re-read on mtime change. Rotation can happen without restart.
- Schema-per-project isolation — mentioned in the design doc as useful for scenario 2, and it already exists in substrate's Postgres backend.

**Not supported (design doc assumes or needs):**

- **Asymmetric signing (BC-196).** `_signing.py` is HMAC-only. `compute_hmac` (line 29) and `verify_hmac` (line 37) are the only primitives. No `sign_ed25519`, no `verify_ed25519`, no `alg` discriminator on events. This is the hard blocker for the auditor model — auditors cannot verify without holding the HMAC secret.
- **Key fingerprint computation.** `_keys.py:KeyEntry` has `key_id: str` (an arbitrary label) and `secret: bytes` (the HMAC key). There is no method to compute a cryptographic fingerprint of a public key. For ed25519, this would be `hashlib.sha256(public_key_bytes).hexdigest()`.
- **Actor-to-key binding.** `actor_id` in the signing envelope (`_signing.py:13`) is a caller-provided string. Nothing in substrate binds `actor_id` to `key_id`. Any holder of the HMAC key can claim any `actor_id`. With asymmetric signing, the binding is implicit (the public key *is* the identity), but with HMAC it's purely conventional.
- **Timestamp in the signed payload.** `_signing.py:build_signing_envelope` does not include the event timestamp. The timestamp is set at `_event_store.py:88` (`datetime.now(UTC)`) *after* signing. This means the timestamp is not integrity-protected. An operator with the HMAC key can backdate events. BC-198 (operator forgery) is the tracked gap.
- **CLI keygen.** No command exists to generate an ed25519 keypair or an HMAC secret and write it to the key file format. The key file is assumed to already exist.

## 3. Position on Question A: Identifier format on the wire

**Recommendation: Opaque ID + key fingerprint, with the binding as the identity primitive.**

The candidates, assessed against the signing envelope in `_signing.py:10-26`:

**Raw key fingerprint** — The signing envelope already has `actor_id` (string) and the `Event` object has `key_id` (string). A raw fingerprint replaces `actor_id` entirely. Changes to substrate: add a `fingerprint()` method on `KeyEntry`, use it as `actor_id`. Minimal invasion. But it forecloses the opaque-ID path: an enterprise deployment that wants `actor_id = "alice"` (mapped via OIDC) can't have it without a second identifier field.

**Opaque ID + attestation-type enum** — The design doc's proposed position (line 292-303). The signing envelope's `actor_id` field becomes the opaque ID. A new field (`attestation_type`) is added to the envelope or to `on_behalf_of`. The key fingerprint is stored separately (either as a new envelope field or as part of `on_behalf_of`). Changes to substrate: add `attestation_type` to the signing envelope, add `key_fp` as a new field (either in the envelope or in `on_behalf_of`), add `fingerprint()` to `KeyEntry`. Moderate invasion — the envelope shape changes, which means `build_signing_envelope` changes, which means `verify_event`'s backward-compat logic (line 92-98) needs another branch.

**DID URI** — `did:key:z6Mku...` bakes the key type and key bytes into the identifier. The signing envelope's `actor_id` becomes the DID. Changes to substrate: add a DID parser/serializer, add DID resolution (at minimum, `did:key` extraction of the public key), change the key file format to store DID documents instead of raw secrets. Heavy invasion. The `KeyEntry` dataclass needs restructuring. The signing envelope semantics change (the identifier *is* the key, so `key_id` becomes redundant).

**My position: Opaque ID + key fingerprint, but with a correction to the design doc.**

The design doc proposes `{id, attestation_type}` with verification material in a side-channel registry. The opencode agent reader input proposes `{id, key_fp, binding}`. The second is better, because the key fingerprint is not a side-channel concern — it's the thing the signature actually proves. Here's why:

The signing envelope covers `actor_id` (the opaque ID). It does not cover `key_id` (per `_signing.py:10-26`). But the *verifier* needs to know which key signed the event. The `Event` object has `key_id`, but that's stored alongside the signature, not inside it. With asymmetric signing, the verifier can extract the public key from the signature itself. With HMAC, the verifier needs `key_id` to look up the key. Either way, the binding "this key is authorized to sign as this actor" is the identity primitive, and it must be verifiable.

The minimal substrate changes for this option:
1. Add `fingerprint()` to `KeyEntry` in `_keys.py` (5 lines).
2. Add `key_fp` field to `on_behalf_of` or to a new `identity` sub-object in the signing envelope (touches `build_signing_envelope`, `verify_event` backward-compat).
3. Define the `on_behalf_of` or `identity` schema with `{id, key_fp, attestation_type}` (extends `validate_delegation_chain` in `_contract.py`).

This is more invasive than raw fingerprint but less invasive than DID. It preserves the auditor model: the auditor receives the event, extracts `key_fp`, looks up the public key (from a registry or from the DID document if the `attestation_type` is `did`), verifies the signature. The opaque `id` is human-readable metadata; the `key_fp` is the cryptographic anchor.

## 4. Position on Question B: Auditor access model

**Recommendation: Durable audit bundles, with direct verification as an implementation detail.**

The driving use case from `agent-provenance/README.md:199`: "the project owner wants to use AI agents at work in a regulated context." The specific compliance regimes implied:

- **SOC 2** (line 26, via BC-196): Auditors require evidence of controls. They want a sampled, time-bounded artifact they can review. They do not query your database. They want a bundle.
- **HIPAA** (line 26, via BC-196): Same pattern — auditors want documentation of access controls and audit trails, delivered as evidence packages. They do not have production credentials.
- **Internal IT audit** (the "workplace-audit" use case): The compliance team wants a periodic report they can file. They are not going to run `substrate verify` against a live Postgres instance.

In all three cases, the auditor's actual workflow is: receive a signed artifact, verify it, file it. Direct verification against a live system is a development/debugging convenience, not a compliance primitive.

The durable audit bundle is:
- A signed export of a time-bounded segment of the event log.
- Includes the signing keys (public keys) of all actors who signed events in the segment.
- Includes the delegation chains (`on_behalf_of`) for all events.
- Signed by the export key (a substrate key that is not any actor's key — it's the system's attestation key).
- Verifiable offline by anyone who has the export key's public key.

This composes with the existing substrate infrastructure:
- `_replay.py` already replays events from a log segment. The bundle generator is a replay that exports + signs.
- `_keys.py` already manages multiple keys. The export key is just another key entry with a distinct `key_id`.
- The signing envelope (`_signing.py`) can be reused to sign the bundle itself.

Direct verification is still useful — it's how the project owner tests during development, and it's how a technical auditor with database access would do spot-checks. But it's an implementation detail, not the access model. The access model is bundles.

**Why not hybrid:** The hybrid adds complexity (two verification paths, two documentation surfaces) for a marginal benefit. If an auditor has direct access, they can still use the bundle-verifier tool against live data — the verification logic is the same. The bundle is the interface; direct verification is an optimization.

## 5. Position on Question C: Recovery mechanism

**Recommendation: Second-device recovery key for v2, design now.**

Assessment of each option against v1 schema constraints:

**Second-device recovery key** — At setup, generate a second ed25519 keypair. Store it offline. The recovery key can sign a `key_rotation` event that replaces a lost primary key. V1 changes needed: add `recovery_key_fp` field to the key registry (or to a separate recovery metadata file). The signing envelope does not change. The `on_behalf_of` schema does not change. The `KeyEntry` dataclass gains an optional `recovery_key_fp` field. The rotation event schema (which doesn't exist yet but will be a signed event) needs a `recovery_signed: bool` field to distinguish "rotated by primary key" from "rotated by recovery key." This is backward-compatible: v1 events without `recovery_key_fp` are simply unrecoverable, which is the documented behavior.

**Social recovery** — N-of-M peers co-sign a key replacement. This requires multi-signature verification in the signing envelope. `_signing.py:sign_event` returns a single `(signature, canonical_hash, envelope)` tuple. N-of-M would require an array of signatures. This is a change to the signing envelope shape, which is the most invasive possible change. It also requires a peer registry (who are the M peers?) and a threshold configuration. V1 changes: the signing envelope would need to support multi-sig from the start, even though v1 doesn't use it. This is over-engineering for v2 prep.

**OIDC-only** — Recovery is the IdP's problem. For enterprise: user authenticates with OIDC, IdP vouches for the new key. V1 changes: the `on_behalf_of` schema already has `principal_id` and `authenticated_at`. To support OIDC recovery, you'd need an `oidc_recovery_token` field or similar — the user presents an OIDC ID token as proof of identity, and substrate accepts it as authorization to rotate the key. This is an IdP integration, which BC-197 remedy (4) explicitly defers. V1 can document "OIDC recovery is the IdP's problem" without changing any schema. It's the zero-cost option for v1.

**No recovery, ever** — Document and accept. This is the simplest and is the current implicit behavior. But it precludes nothing: if you later add recovery, you add it to a system that already has `key_id` and key lifecycle states. No schema changes needed to accept it now.

**My position: Second-device recovery key, designed now, built in v2.**

The v1 schema change is minimal: add `recovery_key_fp: str | None` to the key registry format (the JSON file that `_keys.py:_load` reads). Add it to `KeyEntry` as an optional field. Do not add any signing or verification logic for it in v1. Document that v1 keys are unrecoverable, and that `recovery_key_fp` is a v2 feature.

This does not preclude social recovery (which would be an additional mechanism, not a replacement) or OIDC recovery (which is orthogonal). It does not require multi-sig in the signing envelope. It does not require any change to `_signing.py`.

The specific v1 schema addition:

```python
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
    recovery_key_fp: str | None = None  # v2 prep, ignored in v1
```

And in the key file JSON:

```json
{
  "keys": [
    {
      "key_id": "primary-001",
      "secret": "...",
      "status": "active",
      "recovery_key_fp": "sha256:def456..."
    }
  ]
}
```

This is a one-field addition to `_keys.py` and the key file format. It changes nothing about signing, verification, or the event schema.

## 6. Disagreements with consensus

**The "auditors are identities too" position (Q6, line 348-354) is under-specified.** The position says "No special 'auditor' role type. An auditor is simply another identity that has been granted read access." But the same position says auditor attestations are signed events in the log. If an auditor signs an attestation event, they need a signing key — and that signing key needs to be verifiable. Under the current design, that means the auditor has an ed25519 keypair and their attestation events are signed with it. But the auditor's key is not in the same key registry as the actors' keys (the position says "a simpler mechanism — a separate keyring"). This creates two key registries, two verification paths, and two trust models. The design should pick one: either auditors use the same key infrastructure as actors (same registry, same signing envelope, same verification), or they use a completely separate mechanism (GPG signatures on exported bundles, for instance). The current position hand-waves this.

**The Q7 position says "Recovery: at setup time, generate a second 'recovery key'" (line 548-550).** But the "Proposed positions" section says "Recovery: out of scope for self-sovereign mode" (line 366-368). These contradict. The agent positions and the proposed positions were written by different reviewers, and the design doc doesn't resolve the contradiction. My position above (second-device recovery key, designed now) is closer to the agent positions but with a narrower v1 scope.

## 7. Things the design doc misses entirely

**The timestamp gap.** The signing envelope (`_signing.py:10-26`) does not include the event timestamp. The timestamp is set at `_event_store.py:88` after signing. This means an operator with the HMAC key can create events with arbitrary timestamps. BC-198 tracks this, but the design doc doesn't mention it. For the auditor model, this is critical: an auditor verifying "when did this happen" cannot trust the timestamp if it's not in the signed payload. The fix is to add `timestamp` to `build_signing_envelope`. This is a one-line change to `_signing.py` but it breaks backward compatibility with all existing signed events (the backward-compat retry at line 92-98 would need to handle the missing timestamp case).

**The actor-to-key binding gap.** The design doc discusses identity at length but never addresses the fact that `actor_id` in the signing envelope is a caller-provided string with no cryptographic binding to the signing key. Anyone with the HMAC key can claim any `actor_id`. With asymmetric signing, the binding is implicit (the public key proves who signed). But the design doc doesn't note that asymmetric signing is what *makes* the identity primitive trustworthy — without it, `actor_id` is self-attested, which is exactly what BC-101 warns about.

**Multi-key signing for the delegation chain.** BC-197's remedy (3) says "Per-principal signing keys. Each principal in the delegation chain independently signs the canonical payload. Verifier checks N signatures." The design doc's dual-identity model (trigger + actor) is a two-link chain. If both links need independent signatures, `_signing.py:sign_event` needs to return two signatures (or N signatures). The current function returns one. This is a structural change to the signing interface, and the design doc doesn't address it.

**Key export format for auditors.** The design doc says auditors hold public keys. But the key file format (`_keys.py` JSON schema) stores `secret` (bytes). There is no export mechanism for public-only key material. For asymmetric signing, the public key is derivable from the secret key. But there's no code to do this derivation or to export a public-key-only file. Auditors need a way to receive public keys without receiving secrets.

**The scope attestation from `agent-provenance/README.md:47-51`.** The provenance README says "the scope itself must be a signed first-class artifact in the event log — an attestation that declares 'this substrate instance captures events from configured harnesses A, B, C; other sources are out of scope.'" This is not mentioned in the identity design doc at all. It's a provenance-layer concern, but it interacts with identity: who signs the scope attestation? Is it the operator? A quorum of actors? The design doc should at least acknowledge it.
