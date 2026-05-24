# Design Review: Identity and Multi-User

**Reviewer:** Gemini 3.5 Flash (Gemini Lineage)  
**Date:** 2026-05-23  
**Document Reviewed:** `/projects/agent-wake/design/identity-and-multi-user.md`  

---

## 1. Pre-Reading Attestation

| File / Location | Most Surprising Finding |
| :--- | :--- |
| `/projects/agent-wake/design/identity-and-multi-user.md` | The "Proposed positions" section (lines 290–304) advocates for a flexible `Opaque ID + attestation-type` format, but the "Agent positions" section (lines 417–430) pivots to raw public key fingerprints as the wire format. This contradiction is left unaddressed in the text. |
| `/projects/substrate/src/substrate/_signing.py` | The `verify_event()` function (lines 72–99) already implements a backward-compatibility path (line 93) that falls back and retries envelope reconstruction with `on_behalf_of=None`, showing how carefully the serialization boundary was preserved during the delegation retrofit. |
| `/projects/substrate/src/substrate/_keys.py` | The key status verification method `verify_key_status()` (lines 166–175) raises a hard `SubstrateError` with `ErrorCode.REVOKED_KEY_ID` for `"revoked"` keys, indicating that key revocation is already an active, operational enforcement block rather than future design work. |
| `/projects/substrate/breadcrumbs/resolved/196-hmac-symmetric-no-external-verifiability.md` | The "Non-goals" section (line 125) explicitly decides that HMAC-SHA256 will remain the zero-config homelab default, meaning the pluggable asymmetric signing architecture is designed to be entirely additive and backwards-compatible. |
| `/projects/substrate/breadcrumbs/resolved/197-no-delegation-chain-agent-actor-cannot-bind-to-human.md` | The acceptance criteria (lines 143–164) specify that the `on_behalf_of` sub-object must be validated as part of the *existing* canonical JSON signature path, avoiding the immediate need for complex multi-signature verification schemes in v1. |
| `/projects/agent-provenance/README.md` | The "Honest scope statement" (lines 31–51) dictates that the monitoring scope itself must be recorded as a signed, first-class event in the log so that auditors can cryptographically verify *completeness* (e.g., detecting missing events) rather than just integrity. None of the nine settled questions address this. |

---

## 2. Substrate Code Findings

### What Substrate Already Supports (Which the Design Doc Treats as Future Work)

1. **Delegation Chain Schema and Verification (`_signing.py` & `_contract.py`):**  
   The design doc discusses the propagation and schema of `on_behalf_of` as if they are pending implementation. In reality, **Plan 010 (BC-197)** has already landed:
   * `build_signing_envelope()` in `_signing.py:10-26` includes `on_behalf_of` directly in the dictionary that is serialized via RFC 8785 canonical JSON (`canonicalize(envelope)`) and then HMAC-signed.
   * `validate_delegation_chain()` in `_contract.py:582` enforces that `principal_id` is a non-empty string and that optional parameters like `scope` (list of strings) and `authenticated_at` (string) conform to strict types.
   * `verify_event()` in `_signing.py:72` natively verifies envelopes containing the `on_behalf_of` payload.

2. **Key Lifecycle and Revocation Enforcement (`_keys.py`):**  
   Key lifecycle states (`active`, `deprecated`, `revoked`) are fully implemented and checked:
   * `KeyEntry` in `_keys.py:17` contains a `status: str` field.
   * `verify_key_status()` in `_keys.py:166-175` throws `ErrorCode.REVOKED_KEY_ID` if a key has been marked `"revoked"`, and prints warnings for `"deprecated"` keys.
   * `KeySet._maybe_reload()` in `_keys.py:122` monitors file modification time (`mtime`) and hot-reloads key configurations dynamically at runtime.

---

### What Substrate Does Not Support (Which the Design Doc Assumes)

1. **Asymmetric Cryptography / Pluggable Algorithms (BC-196):**  
   The design doc's auditor verification model relies on auditors verifying logs using public keys. However, `_signing.py` strictly hardcodes HMAC-SHA256:
   * `compute_hmac()` (`_signing.py:29`) and `verify_hmac()` (`_signing.py:37`) rely on symmetric keys.
   * `KeyEntry` (`_keys.py:17`) only holds `secret: bytes`, representing a shared secret. Under the current codebase, an auditor would need the secret key to verify the log, which collapses the adversarial trust boundary because holding the secret allows the auditor (or anyone) to forge past events.

2. **Pluggable Signature `alg` Discriminator:**  
   There is no algorithm identifier field (e.g. `alg: "Ed25519"`) in the envelope dict built by `build_signing_envelope()` (`_signing.py:10-26`), which is a hard prerequisite for supporting both HMAC and asymmetric keys.

3. **Key Generation and Provisioning CLI:**  
   `_keys.py` only loads keys from local JSON files or environment variables. No cryptographic key generator or CLI tooling exists to provision new keys or output rotation events.

---

## 3. Position on Question A: Identifier Format on the Wire

**Position:** **URI-Prefixed Opaque String** (A highly optimized variant of **Candidate 2**).

Instead of representing the identifier as a structured dictionary `{id, type}` on the wire (which would break the current schema and require retrofitting `validate_delegation_chain()` in `_contract.py:590`), we should represent the identifier as a **URI-prefixed string** (e.g., `key:sha256:abc123...`, `oidc:google:sub-123`, `did:key:z6Mku...`).

```
on_behalf_of:
  principal_id: "key:sha256:8f3a9b..."
```

### Code-Grounded Justification

1. **Zero Code Invasiveness for Schema Validation:**  
   Currently, `_contract.py:590-595` enforces:
   ```python
   principal_id = on_behalf_of.get("principal_id")
   if not isinstance(principal_id, str) or not principal_id:
       raise SubstrateError(
           ErrorCode.INVALID_ARGUMENT,
           "on_behalf_of.principal_id is required and must be a non-empty string",
       )
   ```
   A URI-prefixed string is a valid Python `str`. Choosing this format requires **zero modifications** to the existing delegation validation code, database serialization tables, or type-checking logic in `_contract.py`, `_types.py`, and `_in_memory.py`.

2. **Extensibility & Decoupled Trust Models:**  
   * **Candidate 1 (Raw key fingerprint)** forces the system to assume every principal is a public key, which blocks OIDC integration and enterprise SSO. It also couples the identifier to the specific hash of the signing key.
   * **Candidate 3 (DID URI)** introduces massive spec surface and requires network-bound DID document resolvers. DIDs are incompatible with the simple local dict lookup in `_keys.py:get_key()` (`self._keys.get(key_id)`).
   * **URI-Prefixed Strings** keep the protocol layer simple. The scheme prefix (`key:`, `oidc:`, `did:`) acts as the attestation-type discriminator. The verifier parses the prefix and dispatches the verification to the appropriate offline handler without substrate needing to manage complex IdP or DID client libraries.

---

## 4. Position on Question B: Auditor Access Model

**Position:** **Strict Hybrid Model** (Direct verification for monitoring, Durable Audit Bundles for compliance).

The workplace-audit use case described in `agent-provenance/README.md` implies rigorous compliance regimes (such as SOX §404, SOC 2, HIPAA, and internal IT audits) that demand a clear separation of concerns:

1. **SOX §404 & SOC 2 (Evidence Independence):**  
   External auditors cannot rely solely on live queries to an auditee's operational database. The auditee controls the environment and could theoretically manipulate the database or backdate records if they hold the signing keys. The auditor needs "sealed" point-in-time evidence.
2. **HIPAA §164.312(b) (Audit Controls):**  
   Auditors look for archived, immutable artifacts that can be preserved offline. Granting external auditors live read-only access to operational systems (Direct Verification) is a major network security and liability risk that enterprise security teams will reject.

### How the Hybrid Solves This

```
[Agent Session / Live Database] 
       │ 
       ├─► (Real-Time Monitoring) ──► Direct Verification (SOC, alerts)
       │ 
       └─► (Export Event Segment) ─► Durable Audit Bundle (.json)
                                              │
                                              ▼
                                   Add RFC 3161 Timestamp
                                              │
                                              ▼
                                   [Offline Verification]
```

* **Direct Verification** is used internally for operational monitoring, automated alerts, and real-time security operations (SOC). The live log is validated directly against the actor keys.
* **Durable Audit Bundles** are the compliance artifacts. The operator exports signed JSON segments from substrate and anchors them using an **RFC 3161 trusted timestamp token** (which `BC-198` note 189 states is implementable today against HMAC). The auditor verifies these bundles *offline* using a separate CLI tool and the public keys exchanged during bootstrap. This satisfies the "AWS CloudTrail log-file integrity validation" analog cited in `agent-provenance/README.md:24`.

---

## 5. Position on Question C: Recovery Mechanism (v2 Design)

**Position:** **Candidate 1 (Second-device recovery key), designed as a v1 schema metadata field, implemented in v2.**

We must design now so we do not preclude recovery later. 

### Constraints and Code-Grounded Justification

1. **Candidate 2 (Social recovery)** requires co-signatures (N-of-M). This would require rewriting `verify_event()` in `_signing.py:72` to accept and verify multiple signatures, which is a massive retrofit of the v1 single-signature layout.
2. **Candidate 3 (OIDC-only)** couples the self-sovereign OSS model entirely to external identity providers, contradicting the core architectural principles.
3. **Candidate 1 (Second-device recovery key)** fits seamlessly into the existing key architecture with **no schema changes to the signing envelope**.

### V1 Retrofit to Enable Candidate 1

In v1, we only need to add a `role` field to `KeyEntry` in `_keys.py:17`:

```python
# src/substrate/_keys.py (Proposed v1 addition)
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
    role: str = "primary"  # "primary" | "recovery"
```

### The V2 Recovery Workflow

In v2, the recovery key (stored offline on USB or printed QR) is used to sign a standard `key_rotation` event:

$$\text{EventSignature}_{\text{RecoveryKey}} \left( \text{KeyRotationPayload(OldKey, NewKey)} \right)$$

Because `verify_event()` in `_signing.py:72` already verifies an envelope signature against a single `key_id`, a recovery-signed rotation event is structurally identical to any other event. The verifier simply checks that the rotation payload is signed by a key whose `role` in `KeyEntry` is `"recovery"`. This completely avoids multi-signature complexity while ensuring identity continuity in the audit log.

---

## 6. Disagreements with Consensus

### Disagreement A: Settled Position #7 ("No recovery magic") is a Compliance Risk

The consensus position that self-sovereign users should simply "accept identity loss and create a new identity" (lines 367–370) is a major audit failure. 

In a regulatory audit, **identity continuity** is vital. If an operator loses their key and registers a new, unrelated key, they create a gap in the provenance trail. An auditor cannot cryptographically prove that `Alice_Old` and `Alice_New` are the same physical person. 

A **second-device recovery key** is not "recovery magic." It is a standard cryptographic primitive (a backup Ed25519 keypair) that provides a verifiable link between the old and new identities, preserving the continuous chain of custody.

### Disagreement B: Settled Position #6 Conflates Actor and Auditor Roles

The consensus states that "auditors are identities too" and do not need a separate role primitive. While they use the same cryptographic signature mechanisms, their operational risk profiles are entirely different:
* A compromised **actor key** allows an attacker to forge *actions* (which will be logged).
* A compromised **auditor key** allows an attacker to forge *attestations* (which can hide malicious actions entirely).

We must distinguish them. The `KeyEntry` dataclass should carry an explicit `capability` or `role` property (e.g. `role: "actor" | "auditor"`). The verifier must reject any tool-call events signed by an auditor's key, preventing critical privilege escalation where an auditor tries to execute agent actions.

---

## 7. Things the Design Doc Misses Entirely

### 7.1 Completeness: Gaps and the Scope Attestation Event
As `agent-provenance/README.md` §2 highlights, the hardest problem in auditing is *missing events*—where an operator routes agent tool calls around the configured harness. 
To satisfy adversarial audits, the system needs:
1. **Scope Attestation Event:** A signed configuration event logged at session initialization that defines exactly which harnesses, directories, and endpoints are active and within the audit scope.
2. **Harness Heartbeats:** Configured harnesses must emit periodic, signed "keep-alive" heartbeat events. If an operator bypasses the harness or disables logging, the auditor's offline verifier will detect the missing heartbeats and raise a critical alert.

### 7.2 The Trust-Root Bootstrap Problem
The peer model (Settled Position #3) assumes public keys are exchanged out-of-band, but ignores the bootstrap problem. If an auditor receives a log bundle, how do they verify that the actor's public key actually belongs to the human actor? If they ask the operator, the operator could provide a fraudulent public key to validate a backdated, forged log. 
The design must define a **bootstrap protocol** (e.g. mapping key fingerprints to DNS TXT records, GitHub SSH key directories, or signing them with an enterprise SSO certificate).

### 7.3 Forward-Compatibility and Fail-Safe Rules for `alg`
When **BC-196** lands and introduces the `alg` field to denote the signature algorithm, every downstream consumer (like `agent-provenance`) must be prepared to handle it.
The design doc lacks a **fail-safe parsing contract**. We must establish that if a verifier encounters an event signed with an unknown or unsupported algorithm (e.g. a post-quantum algorithm introduced in a future version), it must **fail-shut (reject validation)** rather than warning and skipping.

### 7.4 Correlation and Validation of Multiple Timestamps
Under a full deployment, an event will have three distinct timestamps:
1. `authenticated_at` in the `on_behalf_of` delegation chain.
2. `timestamp` in the substrate event header.
3. The cryptographic timestamp in the **RFC 3161** token.

The design doc never defines the correlation rules between these. We must specify validation constraints (e.g., `authenticated_at` $\le$ `event.timestamp` $\le$ `RFC 3161 timestamp` $+$ tolerance window) to prevent session replay attacks and log backdating.
