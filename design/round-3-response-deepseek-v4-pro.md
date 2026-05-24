# Round 3 final response — identity and multi-user

**Reviewer:** deepseek-v4-pro
**Date:** 2026-05-23
**Role:** Final-round reviewer — close the design space, answer new questions, last
chance to challenge locked positions.

---

## 1. Pre-reading attestation

| File | Most surprising finding |
|------|------------------------|
| `design/identity-and-multi-user.md` | The opencode agent reader's Q6 (line 509-519) argues auditors *don't* need the identity primitive, using stronger language than the "Proposed positions" Q6 (line 348-354) which says auditors *are* identities. These disagree in substance, not just emphasis. The round-2 prompt says Q6's agent-position dissent is the "cleanest dissent" — but the opencode reader's position is more useful because it proposes a concrete alternative (separate keyring, separate signature format). |
| `design/round-2-response.md` | Section 7a (session identity) identifies a genuine blind spot: `DelegationChain.session_id` exists but is unused; wake events target sessions; provenance is scoped to sessions. This maps directly to Kimi's agent-key-vs-human-key observation but wasn't connected in Claude's review — the pieces were adjacent but not joined. |
| `identity-and-multi-user-review-deepseek-v4-pro.md` | The argument that raw key fingerprint "appears minimal but isn't" because BC-196's `alg` discriminator is required regardless (section 3) is the tightest case against candidate 1. My round-2 self made the right call. |
| `review-kimi-k2.6-turbo.md` | The agent-key topology (section 7.1) is the strongest single observation across all seven reviews. Citing `_signing.py:sign_event()` taking exactly one `key: bytes` as the structural constraint — not a design preference but a code-level fact — makes this actionable rather than philosophical. |
| `review-qwen3.6-27b-identity-multi-user.md` | "Scope attestation is versioned, not one-time" (section 7E) is the concise framing the design doc missed. The point that BC-196 is the actual blocker, not identifier format (section 7A), is correct but not new — Kimi and Claude both made it first. |
| `reviews/identity-and-multi-user-gemini-3.5-flash.md` | The URI-prefix encoding proposal for Question A (section 3) is the cleanest implementation path. It requires zero changes to `_contract.py:590` which already accepts bare strings. The `key:sha256:...` / `oidc:...` / `did:...` format encodes attestation-type into the string itself with no schema change. This should be adopted as the implementation choice. |
| `reviews/identity-and-multi-user-glm-5.1.md` | The observation that `KeyEntry.secret: bytes` is algorithm-untyped (section "Code-level gap") is correct and not discussed elsewhere. BC-196's `alg` discriminator needs to land at the `KeyEntry` level (`_keys.py:19`), not just at the event level. HMAC key bytes and Ed25519 private key bytes are indistinguishable without a `kind` or `alg` field on the key entry itself. |
| `breadcrumbs/resolved/196-*.md` | The "Non-goals" section (line 125) explicitly decides HMAC-SHA256 stays as the homelab default. Asymmetric signing is additive and backwards-compatible. This directly constrains Q11 (agent key topology) and Q6 (auditors) — until BC-196 lands, both are structurally limited to HMAC's single-signer model. |
| `breadcrumbs/resolved/197-*.md` | The `on_behalf_of` sub-object (line 112-115) already carries `principal_id`, `session_id`, `authenticated_at`, and `scope`. These are exactly the fields of a Kimi-style session grant. Substrate already has the schema slot; the cryptographic enforcement is the missing piece. |
| `breadcrumbs/resolved/198-*.md` | Line 189: "Layer 1 is implementable today against the existing HMAC primitive and should land first." This was cited in two prior reviews but neither explored the consequences: it means temporal integrity (Q12) can ship before asymmetric signing, which changes the v1-v2 ordering assumptions. |
| `_signing.py` | `build_signing_envelope()` (line 18-25) covers `event_id`, `work_item_id`, `actor_id`, `on_behalf_of`, `transition`, and `payload`. It does NOT cover `timestamp`, `key_id`, or `event_seq`. The `_event_store.py:88` timestamp is set after signing — a gap flagged by 5/7 reviewers. |
| `_keys.py` | `_load()` at line 94 sets `new_active` to the *first* `active` key encountered. But the load loop iterates all keys and adds them all to `new_keys` regardless of status. Multiple `active` keys in the JSON file would all be loaded; only the first becomes `_active_key_id`. This means a recovery key with `status: "active"` would be a valid key in the `KeySet` — just not the one `active_key()` returns. The key file can already hold multiple active keys without schema changes. |
| `_types.py` | `ActorRole` (line 651) is a separate dataclass from `KeyEntry`. It binds `actor_id` to a workflow `role` (e.g., "reviewer", "approver") with a `created_at` timestamp. This is the substrate-native mechanism for role assignment within a workflow — it is *not* the same thing as a cryptographic key capability. The design doc conflates these when discussing Q6 auditor roles. `ActorRole` lives in the event log; key capabilities live in the key registry. Different layers. |
| `_types.py` | `DelegationChain.session_id` (line 67) is typed as `str | None` but `validate_delegation_chain()` in `_contract.py:582-613` never validates it. The field is accepted on input and round-tripped — but never type-checked, length-checked, or format-checked. This is a validation gap that matters for Q11 (session grants). |
| `_event_store.py` | Line 70-81: when `key_set is not None`, exactly one key is used (`key_set.active_key()`). There is no path to sign with a non-active key. A recovery key or a session-grant key would need `active_key()` to return it, or the signing path would need to accept an explicit `key_id` override. |
| `agent-provenance/README.md` | Lines 31-51 (section 2) are the clearest articulation of the scope problem. "The scope itself must be a signed first-class artifact in the event log" — this is a requirement with no implementation sketch, no schema proposal, and no answer to "who signs it." Five prior reviewers flagged it; none proposed a concrete schema. |

---

## 2. Substrate code findings beyond what prior reviewers caught

### Finding 1: Multiple active keys are already supported in the key file format

`_keys.py:89-95` loads every key from the JSON array into `new_keys` regardless of status. The `new_active` variable is set to the first `active` key encountered (line 94-95), but all keys — including multiple `active` ones — are added to the dict. If a key file contains:

```json
{
  "keys": [
    {"key_id": "primary-1", "secret": "...", "status": "active"},
    {"key_id": "recovery-1", "secret": "...", "status": "active"}
  ]
}
```

Both keys are loaded and both are `get_key()`-addressable. The recovery key is a fully valid `KeyEntry` in the set. Only `active_key()` returns just the first one (line 149-164). This means a recovery key or a session-grant key can already exist in the key file today — `sign_event()` just needs to be callable with an explicit `key_id` instead of implicitly using `active_key()`. No schema change to `_keys.py` is needed for multiple active keys; only an API change to `_event_store.py:append_event()` to accept an optional `key_id` override.

### Finding 2: `validate_delegation_chain()` silently ignores `session_id`

The `on_behalf_of` dict at `_contract.py:582-613` validates `principal_id` (required string), `scope` (optional list of strings), and `authenticated_at` (optional string). `session_id` — which is an explicitly typed field in `DelegationChain` at `_types.py:67` — is never validated. Any value passes through. An attacker can set `session_id: 12345` (integer) and `validate_delegation_chain()` won't reject it. The field is carried into the canonical JSON envelope at `_signing.py:22` and is HMAC-signed, so the value is integrity-protected — but the shape is unvalidated.

This gap matters for Q11 because a session grant MUST carry a valid `session_id`. Without validation, a malformed `session_id` enters the log and an auditor cannot distinguish "this session_id is invalid" from "this session_id is valid but the session is unknown."

### Finding 3: `ActorRole` (`_types.py:651`) is the existing substrate primitive for role assignment — but it's workflow-scoped, not key-scoped

`ActorRole` binds `actor_id → role` with a `created_at` timestamp. It represents a workflow authorization (e.g., "alice has the `reviewer` role in this workflow"), not a cryptographic key capability. The design doc's Q6 discussion of a `role` field on `KeyEntry` is a *different thing* — a key-level capability that gates what events a key can sign. These two role systems would coexist: `KeyEntry.capability` (or `role`) gates signing; `ActorRole` gates workflow transitions. The prior reviews conflated them.

### Finding 4: `ConnectionInfo.project` (`_types.py:255`) already namespaces substrate instances by project

Substrate already has a project-level namespace: `ConnectionInfo.project: str`. Every substrate instance carries a project name. A scope attestation could reference this project name as its scope namespace — "this substrate instance, identified by project `agent-provenance-workplace`, captures events from harnesses A, B, C." The project name is a natural anchor for the scope statement.

---

## 3. Q6 (reopened): Auditors as identities

**Position: (c) — the middle path, with `capability` on `KeyEntry`, grounded in the existing `ActorRole` / key-role separation.**

### The code already has two role layers

Substrate's `ActorRole` at `_types.py:651` is the workflow authorization layer — it says "alice can perform transitions that require the `reviewer` role." `KeyEntry` at `_keys.py:17` is the cryptographic signing layer — it says "this key material can produce valid signatures." These are currently orthogonal: any key can sign any event for any actor with any role, because `actor_id` in the signing envelope (`_signing.py:13`) is self-attested (BC-101).

The cleanest dissent (Kimi) argues for completely separate auditor infrastructure — different keyring, different verification path, attestations stored outside the log. The consensus argues for no distinction at all — auditors are just identities.

The middle path adds `capability: str = "actor"` to `KeyEntry` with values `"actor"` | `"auditor"` | `"recovery"`. This is not `role` (which is the workflow concept in `ActorRole`); it's `capability` (what events can this key sign). The enforcement point is `verify_key_status()` at `_keys.py:166`, extended to:

```
def verify_key_status(self, key_id: str, required_capability: str | None = None) -> KeyEntry:
    entry = self.get_key(key_id)
    if entry.status == "revoked": raise ...
    if required_capability and entry.capability != required_capability:
        raise SubstrateError(ErrorCode.WRONG_KEY_CAPABILITY, ...)
    return entry
```

### Why this beats the consensus

The consensus says auditors use the same identity primitive as actors. This means an auditor key can sign action events — `sign_event()` at `_signing.py:43` doesn't check who's signing what. The `capability` field closes this gap: an auditor's key (`capability: "auditor"`) is rejected at the signing boundary if it attempts to sign a `tool_call` event. The key material is the same; the lifecycle (rotation, revocation, hot-reload) is the same; only the permitted event types differ.

### Why this beats the dissent

Kimi's separate infrastructure creates two keyrings, two verification paths, and two trust models. This is correct architecturally — the threat models differ — but it's premature for v1. The capability field costs one optional string on `KeyEntry` (3 lines of schema change, ~10 lines of enforcement code in `verify_key_status()`). It doesn't bifurcate the codebase. If the separation needs to widen later (different rotation policies, hardware-backed auditor keys), the capability field is the hook to fork from. Adding a separate keyring later would require a migration of all auditor key entries; adding capability to an existing key entry doesn't.

### Concrete v1 change

```python
# _keys.py:17 — KeyEntry gain
@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    secret: bytes
    status: str
    capability: str = "actor"  # "actor" | "auditor" | "recovery"
```

```python
# _keys.py:72 — _load() validation
status = entry.get("status", "active")
capability = entry.get("capability", "actor")
if capability not in ("actor", "auditor", "recovery"):
    raise SubstrateError(...)
```

This does not touch `_signing.py`, `_contract.py`, or `_types.py`. Enforcement at `verify_key_status()` is additive. A key without `capability` defaults to `"actor"` — backwards-compatible with all existing key files. Auditor attestations go through the same `sign_event()` path; the caller passes the auditor's `key_id` and the event type is semantically an attestation, not a tool call. The `capability` check at signing time is a policy that verifiers can also check offline.

The attestation itself is a signed event in the log (as the consensus says). But the auditor's key is marked `capability: "auditor"` so an offline verifier can distinguish attestation signatures from action signatures by key capability.

---

## 4. New Q10: Scope attestation

### Who signs the scope statement?

The operator, at substrate initialization or at first harness registration. This is the *least* trust-assuming answer: the operator controls which harnesses are configured, so the operator attests to the scope. An auditor who trusts the operator's scope attestation is accepting the operator's representation of what was in scope — exactly the same trust model as "the operator runs the harness." The crypto doesn't replace the trust; it makes the representation falsifiable later.

If scope is later extended and the auditor finds events from a harness that predate the scope-extension event, the scope attestation is falsified — the operator's earlier attestation was false. This is the property that matters, not who signed it.

### Is scope versioned? Scope-rotation events?

Yes. Scope is versioned via chained attestation events. Each scope attestation carries a `previous_scope_event_id` linking to the prior scope. This is exactly the same pattern as key rotation — chained signed events in the log.

Schema proposal:

```json
{
  "type": "scope_attestation",
  "scope_event_id": "<uuid>",
  "previous_scope_event_id": "<uuid | null>",
  "effective_from": "2026-05-23T00:00:00Z",
  "project": "agent-provenance-workplace",
  "harnesses": [
    {"name": "claude-code", "config_digest": "sha256:..."},
    {"name": "opencode", "config_digest": "sha256:..."}
  ],
  "out_of_scope": "All other agent harnesses and manual tool invocation"
}
```

Key design decisions:
- **`effective_from`** is the UTC timestamp from which this scope applies. Events before this timestamp are governed by the previous scope attestation. This handles the "harness added at 2pm" problem.
- **`previous_scope_event_id`** chains the attestations. The first attestation (bootstrap) has `null`. The verifier replays attestations in order — same as key rotation.
- **`config_digest`** is `sha256(harness_config.json)`, matching `agent-provenance/README.md:188`. The auditor verifies that the harness was configured as attested at the time events were captured.
- **`out_of_scope`** is a free-text field. A signed free-text field is less defensible than a structured one, but it captures what formal schemas can't: "we didn't configure Cursor, we didn't configure Aider." The auditor reads it; they can challenge it; they can't change it without breaking the signature.
- **Who signs it:** the operator's main signing key (`actor_id` of the substrate owner). In solo-dogfood, this is the only identity.

### Harness heartbeats — scope for v1?

Gemini proposed periodic heartbeat events from configured harnesses. The proposal is correct — without heartbeats, the auditor cannot detect "operator routes around the harness." But this is a harness-level concern, not a substrate-level concern. Substrate receives heartbeat events; the harness produces them.

For v1: document the heartbeat contract. Each harness configured in the scope attestation SHOULD emit a signed `heartbeat` event at a configurable interval (default: 15 minutes). The auditor's offline verifier detects gaps — if the scope says harness A is configured and no heartbeat from harness A appears for 30 minutes, the verifier flags it.

Implementation: the agent-provenance adapter (the `PreToolUse`/`PostToolUse` hook layer) produces heartbeat events. Substrate just receives them as events. No new substrate primitive is needed — heartbeats are events like any other, with a `type: "heartbeat"` and a `harness_name` field.

**Scope attestation is in v1 scope.** Heartbeats are a v1 schema item (the event type exists) but enforcement is documented as "the verifier checks; substrate doesn't reject missing heartbeats."

---

## 5. New Q11: Agent-key vs. human-key topology

**This is the highest-priority new question. Kimi's observation is correct and actionable.**

### The structural problem

`_signing.py:sign_event()` takes one `key: bytes`. Every event has one signature. The `on_behalf_of` delegation chain says "agent did X on behalf of human H" — but both claim and action are signed by the same key. Under HMAC, this is forgeable. Under Ed25519 (BC-196), if agent and human share a key, it's still forgeable by anyone holding that key. For the delegation chain to be cryptographically meaningful, the agent and human must hold different keys.

Kimi's proposed topology:

```
Human identity key (long-lived, ~/.config/) 
    │
    ▼ signs
Session grant event: {principal_id: "alice", session_id: "sess-1", 
    agent_key_id: "agent-session-1", scope: [...], 
    expires_at: "2026-05-23T22:00:00Z", authenticated_at: "2026-05-23T14:00:00Z"}
    │
    ▼
Agent session key (ephemeral, per-session) 
    │
    ▼ signs
Tool-call events: {actor_id: "alice", on_behalf_of: {...}, 
    session_grant_event_id: "<uuid>"}
```

Verification: check tool-call signature against agent session key → check session grant signature against human identity key → verify grant hasn't expired and scope covers the action.

### Should v1 support this?

**No — v1 designs the schema to permit it.** Building the full two-key verification path requires BC-196 (asymmetric keys) and changes to `_signing.py:verify_event()` to check two signatures. v1 has HMAC and one signature per event.

But v1 CAN and SHOULD reserve the schema slots. Substrate already has them:

- `DelegationChain.session_id` at `_types.py:67` is the session identifier. It's already in the schema; it just needs validation in `validate_delegation_chain()`.
- `on_behalf_of` already carries `principal_id`, `session_id`, `authenticated_at`, `scope` — the four fields of a session grant.
- The missing field is `agent_key_id` in the grant and `session_grant_event_id` in action events.

**V1 action items:**

1. Add `session_id` validation to `validate_delegation_chain()` in `_contract.py:582`. Validate it's a non-empty string. 3 lines.
2. Add `session_grant_event_id` as an optional field to the `on_behalf_of` dict. Validate it's a string when present. 5 lines in `_contract.py`.
3. Add `expires_at` as an optional field to `on_behalf_of`. Validate it's an ISO timestamp string. 5 lines in `_contract.py`.
4. Define the `session_grant` event type in the event schema. It's a first-class event like any other, signed by the human's key, carrying `principal_id`, `session_id`, `agent_key_id`, `scope`, `expires_at`, `authenticated_at`.
5. Document that v1 does not enforce the two-key verification path, but v2 will: the verifier checks the action event's signature against the agent key, then checks the session grant's signature against the human key.

### Session grant lifetime

Time-bounded. The `expires_at` field in the grant is mandatory. The session's natural lifecycle (Claude Code channels close when the process exits; opencode sessions end on `session.deleted`) provides an *operational* boundary, but the cryptographic boundary must be time-based because the verifier has no access to the live process state.

Revocable mid-session: yes, via a signed `session_revocation` event (same pattern as key revocation). The human signs a revocation referencing `session_grant_event_id`. The verifier sees: grant existed, action was within grant scope and time bounds, grant was not revoked before the action. A mid-session revocation is just a revocation event with a timestamp before `expires_at`.

### Interaction with Q6 (auditors)

An auditor's attestation would be signed with the auditor's identity key directly, not with an ephemeral session key. This is because auditor attestations are scoped to the audit, not to a harness session. The auditor's key has `capability: "auditor"` (per my Q6 answer above). The auditor generates a one-time keypair for each attestation (or reuses a long-lived auditor identity key — deployment choice), signs the attestation, and the attestation event enters the log.

This does NOT argue for the Q6 dissent (separate auditor infrastructure). The auditor's key uses the same `sign_event()` path, the same key lifecycle, and the same `KeyEntry` dataclass. The difference is `capability: "auditor"` and the event type (`attestation` vs `tool_call`).

### What about Claude's session-identity observation?

Claude (round 2, section 7a) noted that session identity is conflated with principal identity. The session grant mechanism resolves this: `session_id` in the grant IS the session identity. Wake events target `session_id`; provenance events carry `session_id` via `on_behalf_of`. The `DelegationChain` dataclass already has `session_id` — it's just not validated or enforced. This design ties them together without adding a new primitive: session identity is a first-class concept in the delegation chain, and session grants make it cryptographically meaningful.

### Code constraint: one signature today, two signatures tomorrow

`_signing.py:sign_event()` returns one `(signature, canonical_hash, envelope)` tuple. For v1, each event is one signature. The session grant and the action event are separate events, each with one signature. The verifier replays the log and checks two events together — it doesn't check two signatures on one event.

For v2 (when BC-196 lands and multi-key verification is needed), the verifier logic is:

```python
# Pseudocode for v2 offline verifier
def verify_action_event(event, log):
    agent_key = resolve_key(event.on_behalf_of.agent_key_id)
    if not verify_signature(event, agent_key):
        return FAIL
    grant = log.find_event(event.on_behalf_of.session_grant_event_id)
    if not grant:
        return FAIL  # missing grant
    human_key = resolve_key(grant.actor_id)
    if not verify_signature(grant, human_key):
        return FAIL
    if event.timestamp > grant.expires_at:
        return FAIL  # grant expired
    if grant_was_revoked_before(grant.session_grant_event_id, event.timestamp, log):
        return FAIL
    return OK
```

This doesn't change `sign_event()`. It uses separate events — which substrate already supports.

---

## 6. New Q12: Temporal integrity

### Should `timestamp` be added to the signing envelope?

**Yes.** The fix is `build_signing_envelope()` at `_signing.py:10-26` gaining a `timestamp: str` parameter (ISO 8601), included in the canonical JSON dict.

The backward-compat concern is real but acceptable now. `verify_event()` at line 92-98 already has a retry path for old events without `on_behalf_of`. The same pattern handles old events without `timestamp`:

```python
def verify_event(
    ...,
    timestamp: str | None = None,  # NEW parameter
    ...
) -> bool:
    # ... build envelope with timestamp ...
    if _verify_once(envelope, ...):
        return True
    # Backward compat: old events without timestamp in envelope
    if timestamp is None and stored_envelope is None:
        old_envelope = build_signing_envelope(...)  # without timestamp
        if _verify_once(old_envelope, ...):
            return True
    # Backward compat: old events without on_behalf_of
    if on_behalf_of is None and stored_envelope is None:
        ...
```

This is two additional retry branches. It's ugly but matches the existing pattern. The right time to do this is now — before there are production events with missing timestamps. The alternative (never add timestamp to the envelope) leaves the temporal integrity gap open forever.

The `_event_store.py:88` change: `timestamp` is computed BEFORE calling `sign_event()`, passed through `append_event()` to `sign_event()`, and included in the envelope. The event store still sets `now = datetime.now(UTC)` but passes `now.isoformat()` into the signing call.

### Relationship between three timestamps

Three timestamps exist in a fully-deployed system:

1. `authenticated_at` — in `on_behalf_of`, set by the caller. Represents when the human authenticated.
2. `event.timestamp` — in the signing envelope and event header. Set by the event store at append time. Represents when substrate received and signed the event.
3. RFC 3161 token timestamp — from an external TSA. Represents when the TSA attests the event batch existed.

**Ordering constraint:**

```
authenticated_at <= event.timestamp <= TSA.timestamp + tolerance
```

Where tolerance is configurable (default: 5 minutes) to account for clock skew between substrate's server and the TSA.

- `authenticated_at > event.timestamp` means the event was signed before the human allegedly authenticated — impossible. Reject at verification.
- `event.timestamp > TSA.timestamp + tolerance` means the event was allegedly signed after the TSA attested the batch — impossible. Reject at verification.
- `event.timestamp <= TSA.timestamp` is the normal case: event was signed, then included in a batch submitted to the TSA.

**Who enforces this?** The offline verifier tool. Substrate's `_event_store.py:append_event()` can enforce `authenticated_at <= event.timestamp` at append time (cheap string comparison). The TSA constraint is enforced by the verifier, which has access to both the TSA token and the event log.

### Who provides the TSA? Batch interval?

**TSA:** configurable per deployment. V1 ships with FreeTSA (`https://freetsa.org`) as the default. The configuration supports multiple TSA URLs; the verifier accepts any one of them. This matches BC-198's acceptance criterion (line 169): "Multiple TSAs can be configured; substrate stores tokens from each and the verifier accepts any one of them."

For SOX/enterprise: the TSA URL is a config option. If the auditor requires a specific TSA they trust (DigiCert, GlobalSign, an industry-specific TSA), the operator configures that TSA. The default FreeTSA is for dogfood and homelab; the documentation says "for regulated deployments, configure a TSA your auditor accepts."

**Batch interval:** configurable, default 15 minutes. BC-198 (line 168) says "every N events, every M seconds, on-demand." The default of 15 minutes is a practical middle ground: per-event is too expensive for TSA round-trips; hourly is too coarse for detecting "operator signs a fabricated event and timestamps it 59 minutes later to look plausible." 15 minutes means fabricated events can be at most 15 minutes stale before the TSA window exposes the backdating attempt.

Configurable via `SUBSTRATE_TSA_BATCH_INTERVAL_SECONDS` environment variable. V1 supports `every_N_events` and `every_M_seconds` modes; `on-demand` is a v2 feature.

---

## 7. New Q13: Trust-root bootstrap

### The problem

Peer model says users exchange public keys out-of-band. But an auditor receiving a bundle needs the actor's public key. Asking the operator is circular — the operator can provide a fake public key, sign a fake bundle with the corresponding private key, and the auditor verifies successfully. The auditor needs the key through a channel the operator does not control.

### V1 bootstrap: solo-dogfood as a special-cased trust assumption

For v1, the solo-dogfood case (scenario 1: operator IS auditor) has a degenerate bootstrap: the auditor generates the keypair themselves (they ARE the operator), or they receive the public key through a trusted channel (same organization, same laptop, same `~/.config/` directory). The trust root is self-derived — there is no external bootstrap problem.

**Documentation contract:**

> **V1 trust model (solo + auditor):** The auditor and operator share the same trust root. The auditor knows the operator's public key because they generated it together (shared `agent-wake identity init`) or received it through an organizational channel they trust (shared directory, shared key file, IT-provisioned key). This trust model is adequate for dogfood, internal IT audit, and the "auditor is a colleague with shared infrastructure access" scenario. It is NOT adequate for arms-length external audit — for that, an out-of-band key distribution mechanism is required, and v2 will provide one.

### V2+ bootstrap mechanism: DNS TXT records

GitHub SSH key directories were proposed, but DNS TXT records are simpler and more general:

```
_provenance.alice.example.com. TXT "key:ed25519:sha256:abc123..."
```

**Why DNS TXT:**
- DNS is independent of the operator's substrate instance. The operator doesn't control the `.example.com` zone's DNS server (their IT department does).
- DNS has existing security properties (DNSSEC for integrity, TLS for confidentiality during resolution).
- It's a standard mechanism — DKIM, SPF, and DMARC use the same pattern for email authentication.
- It doesn't require a separate service. The auditor runs `dig +dnssec TXT _provenance.alice.example.com` to get the key.
- GitHub SSH keys require the auditor to know the GitHub username, which is not always the same as the provenance identity. DNS uses the same namespace as email (which the auditor already knows).

**Alternative for enterprise:** Enterprise SSO certificates (the IdP's SAML/OIDC signing certificate) can serve as the trust root. The auditor already trusts the IdP's certificate (they use it for employee authentication). The operator's key is signed by the IdP as part of the OIDC binding. The auditor verifies the IdP's signature on the key binding. This requires OIDC integration (BC-197 remedy 4, deferred to v2), but when it exists, it solves bootstrap for enterprise deployments.

**GitHub SSH keys** remain a valid alternative for OSS users. The mapping is `ssh-keyscan github.com/<username>` and the auditor cross-references the `principal_id` with the GitHub username in the deployment registry. This is documented as "the simplest path for OSS users" alongside DNS for organizational users.

### Is bootstrap itself a signed first-class event?

Yes. The operator signs a `key_registration` event at key creation time:

```json
{
  "type": "key_registration",
  "principal_id": "alice",
  "key_id": "primary-1",
  "key_fingerprint": "sha256:abc123...",
  "method": "key:ed25519",
  "registered_at": "2026-05-23T14:00:00Z",
  "bootstrap_channel": "dns:_provenance.alice.example.com"
}
```

The `bootstrap_channel` field tells the auditor where to find the key independent of the operator. It's advisory (DNSSEC proves the DNS record; the auditor doesn't need to trust the channel field). The auditor checks: does the key in the DNS TXT record match the `key_fingerprint` in the registration event? If yes, the bootstrap is valid.

For solo-dogfood, `bootstrap_channel` is `"local:self"` or absent, and the auditor's trust model is "I generated this key myself." The verifier tool accepts `bootstrap_channel: "local:self"` with an explicit warning: "Trust root is self-derived. Adequate for internal audit only."

---

## 8. New Q14: Identity continuity across key rotation

### Is the opaque ID stable or does it change on rotation?

**Stable.** The opaque ID (`principal_id: "alice"`) is a deployment-chosen human-readable label. It does not change when the underlying key rotates. This is the answer GLM-5.1 was groping toward when arguing for DIDs — identity continuity across rotation is a real requirement, and DIDs solve it. But DIDs add a discovery layer substrate doesn't need. A stable opaque ID with rotation events in the log solves the same problem with less machinery.

### Where is the binding stored?

In the event log, as signed rotation events. The rotation chain is:

1. `key_registration` event: `{principal_id: "alice", key_id: "key-v1", key_fp: "sha256:abc..."}`
2. `key_rotation` event: `{principal_id: "alice", old_key_id: "key-v1", new_key_id: "key-v2", new_key_fp: "sha256:def...", signed_by: "key-v1"}`
3. Subsequent events carry `key_id: "key-v2"` and `actor_id: "alice"`.

The auditor replays the log, builds a `principal_id → current_key_id` mapping, and uses it to resolve which key to verify against. The rotation event is signed by `old_key_id`, so the chain is self-verifying: verify rotation event with old key's material → old key attests to new key → trust the new key for subsequent events.

### What does an auditor see when verifying historical events?

The auditor replays the log from the beginning. When they encounter an event with `principal_id: "alice"` and `key_id: "key-v1"`, they verify with the key-v1 material (obtained from the key registry or the key_registration event). When they encounter the rotation event, they update their mapping: `alice → key-v2`. Events after the rotation are verified with key-v2 material.

If the auditor receives only a bundle (not the full log), the bundle must include the rotation events that cover the bundle's time range, plus the key_registration event that established the initial binding. The bundle is self-contained: it carries the events, the keys, and the rotation chain. The verifier doesn't need to query a live system.

### Does this push us back toward DIDs?

**No.** The objection stands: DIDs add a discovery layer substrate has no code for and the peer model doesn't need. The rotation chain mechanism described above is:

- **Stored where the auditor already looks** — in the event log, alongside the events it covers.
- **Self-verifying** — rotation events are signed by the old key, which the auditor already trusts (because they verified earlier events with it).
- **No external resolution** — no DID document server, no HTTP fetch, no TTL management.
- **No new substrate primitive** — rotation events are events like any other, signed by `sign_event()`, stored in the event log.

The one honest argument for DIDs (identity continuity across rotation) is solved by the rotation-chain approach without importing DID's ontology. The rotation chain IS the "DID document" — it just lives in the substrate log instead of on a DID server.

GLM-5.1's observation was that DIDs "handle this, which is the one argument for DIDs that the design doc does not engage with honestly." Engaged with now: DIDs solve real problems (rotation, discovery, method agility) but all of them are solvable with substrate's existing event-chain infrastructure. The cost of importing DIDs — a new dependency, new resolution paths, new spec compliance burden — exceeds the benefit for v1.

---

## 9. Last bite at the apple

### Challenge to locked Q2/A: adopt Gemini's URI-prefix encoding as the implementation choice

The locked position says "opaque ID + attestation-type discriminator" with "URI-prefix encoding (`key:sha256:...`, `oidc:...`, `did:...`) is a candidate implementation." I argue it should be the **chosen** implementation, not a candidate.

The evidence: `_contract.py:590` already accepts `principal_id` as a bare string. `validate_principal_id` only checks it's non-empty and printable. URI-prefixed strings satisfy these constraints with zero schema changes. The `method` field on a separate schema adds a new key to `on_behalf_of` and new validation in `validate_delegation_chain()`. The URI-prefix approach adds nothing — the method is parsed from the string by the verifier, not by substrate.

```
# URI-prefix (Gemini proposal, zero schema change):
on_behalf_of: {principal_id: "key:sha256:abc123...", ...}

# Separate fields (consensus proposal, schema change):
on_behalf_of: {principal_id: "alice", method: "key", key_fp: "sha256:abc123...", ...}
```

The URI-prefix approach eliminates the `method` and `key_fp` fields entirely. The verifier parses `principal_id` with a regex: `^(key|oidc|did):(.+)$`. If it matches, the method is group 1 and the identifier is group 2. If it doesn't match, the verifier treats it as an opaque ID with no method (backward-compat with existing events). This is simpler for implementers, requires zero substrate changes, and preserves the same flexibility.

I do NOT challenge the locked position's substance (opaque ID + method discriminator). I challenge the implementation: the method discriminator should be encoded in the `principal_id` string, not in a separate field. This is a pure implementation decision, not a design decision — but it's the kind of decision that, if made wrong, creates schema drift between v1 and v2.

---

## 10. Anything still missed

### 10.1 Substrate-less deployment and signing

`agent-wake/AGENTS.md` says HTTP ingest should work without substrate. GLM-5.1 raised this as a real gap (section "Missing question: What happens when substrate is not present"). The consensus says identity lives in substrate. These conflict.

Resolution: the signing **schema** lives in substrate (as a vendorable contract). A substrate-less deployment implements the same signing schema — canonical JSON envelope, HMAC-SHA256 signature, `on_behalf_of` sub-object — using a standalone library that mirrors `_signing.py` without importing substrate. The agent-wake HTTP adapter signs wake events with this vendored schema before forwarding to the harness.

This means `_signing.py` should be extracted into a standalone package (e.g., `substrate-signing`, published to PyPI as a zero-dependency library) that both substrate and agent-wake depend on. The extraction is a refactor, not a new design — `_signing.py` is already 99 lines with only stdlib dependencies. The only non-stdlib dependency is `_jcs.canonicalize()` at line 26; that module uses `json.dumps()` with RFC 8785 canonicalization, which is also extractable.

**V1 action item:** Extract `_signing.py` and `_jcs.py` into a standalone `substrate-signing` package. Not a design question — an implementation task.

### 10.2 `alg` discriminator forward-compatibility contract

The exact contract hasn't been written. Here it is:

> When a verifier encounters an event with an `alg` value it does not recognize, it MUST fail verification (reject the event) with a clear error message indicating the unknown algorithm. It MUST NOT skip verification, log a warning and continue, or treat the event as unverified-but-ok. Unknown algorithm = untrusted event.

This is fail-shut. The rationale: an attacker who can inject events with a future `alg` value (post-quantum algorithm the verifier hasn't been updated to support) can bypass verification if the verifier falls open. Fail-shut is the only safe default.

This applies to both `alg` on the event and any algorithm discriminator on `KeyEntry`. If `KeyEntry` gains an `alg` field (BC-196), the same rule applies: unknown key algorithm → reject.

### 10.3 GDPR / right-to-erasure collision

Kimi raised this (section 7.3). It's a real problem: append-only signed logs containing personal data conflict with Article 17. The proposed answer is redaction-by-hash:

- Sensitive fields (tool arguments containing filenames with usernames, tool outputs containing personal data) are NOT stored as cleartext in the event log.
- Instead, the event carries `field_hash: sha256(canonical_json(field_value))` in the signed envelope, and the cleartext lives in a separate erasable store.
- The verifier checks `sha256(cleartext) == field_hash` to verify integrity. If the cleartext has been erased (GDPR request honored), the field is marked as `[REDACTED]` in the verification report, but the event's integrity is preserved because the hash still matches the signed envelope.
- The erasable store is a separate database with a deletion API. Substrate does not own it; agent-provenance does.

This is a schema design that must exist before v1 events are produced, because once cleartext is in the signed envelope, erasing it breaks the signature. The schema should reserve `field_hash` fields alongside every `field` field that could contain personal data:

```json
{
  "tool_args_hash": "sha256:abc123...",
  "tool_args_redacted": {"file_path": "/projects/.../..."},
  "result_summary_hash": "sha256:def456...",
  "result_summary": {"stdout_digest": "..."}
}
```

The `_hash` fields are in the signed envelope. The cleartext fields are in adjacent columns in the event store, not in the signed envelope. The verifier reconstructs: hash the cleartext, compare to the signed hash.

This is not a full answer to GDPR — it doesn't address "the auditor can derive personal data from the hashed fields by brute force" — but it's the minimum viable shape for v1. The documentation should state: "v1 provides redaction-by-hash for personal data fields. Full GDPR compliance requires additional measures (encryption at rest, access controls, data retention policies) that are deployment concerns, not substrate concerns."

---

## Summary of positions

| Question | Position |
|----------|----------|
| Q2/A (challenge) | Adopt Gemini's URI-prefix encoding. `principal_id: "key:sha256:..."` — zero schema change. |
| Q6 (auditors) | (c) Middle path: `capability: str = "actor"` on `KeyEntry`. Enforced at `verify_key_status()`. Same key infrastructure, gated event signing. |
| Q10 (scope attestation) | Operator-signed, versioned via chained events. `{type: "scope_attestation", harnesses: [...], previous_scope_event_id, effective_from}`. Heartbeats are a harness concern, schema present in v1, enforcement in verifier. |
| Q11 (agent-key topology) | Schema design now, implementation v2. Add `session_id` validation, `session_grant_event_id` and `expires_at` to `on_behalf_of`. Session grant is a first-class event type. Two-key verification is v2 (needs BC-196). |
| Q12 (temporal integrity) | Add `timestamp` to `build_signing_envelope()`. Ordering constraint: `authenticated_at <= event.timestamp <= TSA.timestamp + tolerance`. FreeTSA default, configurable. 15-minute default batch interval, configurable. |
| Q13 (trust-root bootstrap) | V1: solo-dogfood is self-derived trust, documented as such. V2+: DNS TXT records (`_provenance.alice.example.com TXT "key:..."`). `key_registration` event with `bootstrap_channel` field. |
| Q14 (identity continuity) | Stable opaque ID. Rotation chain in the event log. Verifier follows rotation events. No DIDs — rotation chain is the "DID document" in substrate-native form. |
| Substrate-less signing | Extract `_signing.py` + `_jcs.py` into standalone `substrate-signing` package. |
| `alg` contract | Fail-shut. Unknown algorithm → reject. Never skip or warn. |
| GDPR | Redaction-by-hash. `field_hash` in signed envelope, cleartext in erasable store. Schema designed now, enforcement deferred. |
