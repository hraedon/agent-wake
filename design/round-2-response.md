# Round 2 response — identity and multi-user

**Reviewer:** Claude (Round 2)

---

## 1. Pre-reading attestation

- `identity-and-multi-user.md` — Most surprising thing: BC-197 is written up as
  if it's an open design gap, but the code says it's implemented. The
  `on_behalf_of` field, `validate_delegation_chain()`, and the
  `DelegationChain` dataclass all exist and are tested. The doc hasn't caught
  up to the implementation state.

- `_signing.py` — Most surprising thing: The `on_behalf_of` backward-compat
  retry in `verify_event` (line 93). Old events signed without `on_behalf_of`
  get a retry with `on_behalf_of=None` when `stored_envelope` is absent. This
  is the kind of detail that bites later; it's already handled.

- `_keys.py` — Most surprising thing: The key model has three lifecycle states
  (active/deprecated/revoked) but no `revoked_at` timestamp. Revocation is a
  binary flag, not a temporal claim. This matters for the recovery question
  (Question C) because you can't distinguish "this key was revoked at time T,
  events before T are valid."

- `_types.py` — Most surprising thing: A `DelegationChain` dataclass exists
  (line 64-88) with `principal_id`, `session_id`, `authenticated_at`, `scope`
  — but code search confirms nothing imports or instantiates it. The actual
  delegation field is a raw `dict` in `Event.on_behalf_of`. The typed
  dataclass is dead code (or future scaffolding) waiting for a consumer.

- `breadcrumbs/resolved/196-hmac-symmetric-no-external-verifiability.md` —
  Most surprising thing: BC-196 is `status: accepted`, not `proposed`. The
  acceptance criteria include a pluggable signature interface and Ed25519,
  both unchecked. This is the critical dependency that nothing else works
  without for multi-user, and it's still on the roadmap.

- `agent-provenance/README.md` — Nothing surprising per se, but the
  specificity of §4 (six-layer trust model) and §5 (explicit list of regimes
  they *don't* target: FedRAMP High, DoD/IC) is more precise than the
  identity doc implies. The driving use case is internal workplace audit,
  not external regulatory audit. That distinction matters for Question B.

---

## 2. Substrate code findings

### What substrate already supports that the design doc treats as future work

- **`on_behalf_of` is production-code, not "sketch."** `_signing.py:16` includes
  it in the canonical signing envelope. `_contract.py:582` validates it
  structurally (`principal_id` required, `scope` list-of-strings, etc.).
  `_events.py:103` accepts it on `append_event` and `append_transition_event`.
  It's persisted as JSONB, round-tripped in replay (`_replay.py:300`), and
  exposed in the sidecar. BC-197 is `status: implemented` with 31 tests in
  `test_plan010.py` and `test_plan010_integration.py`. The identity doc's
  framing that "substrate doesn't have delegation" is stale — it's been
  shipped.

- **`DelegationChain` dataclass exists** (`_types.py:65-88`). It's typed,
  frozen, has `to_dict`/`from_dict` round-trip. It's not consumed by any
  code path, but its existence means the schema for delegation is decided —
  `principal_id`, `session_id`, `authenticated_at`, `scope`. No design
  debate needed for the shape.

- **`ActorKind` enum** (`_types.py:13-16`) distinguishes `AGENT`, `HUMAN`,
  `SYSTEM`. This maps directly to the `trigger_identity` vs. `actor_identity`
  distinction in the consensus positions. No new enum needed.

### What substrate does NOT support that the design doc assumes

- **Asymmetric signing (BC-196).** The entire auditor model assumes the
  auditor verifies with a public key without holding the signing secret.
  `_signing.py` is HMAC-SHA256 only — `compute_hmac()`, `sign_event()`,
  `verify_event()` all consume a raw `bytes` key. There's no `alg` field on
  events, no signer interface, no Ed25519 path. The design doc positions say
  "asymmetric signing is the dependency that unlocks multi-user for real" —
  that's correct, and it doesn't exist yet.

- **Key fingerprint or public-key identifier.** `_keys.py` uses opaque
  `key_id` strings (whatever the JSON file names them). `KeyEntry` stores
  `secret: bytes` — for HMAC this is the same key for sign and verify.
  There's no concept of a public key, fingerprint, or keypair. If we want
  raw key fingerprints on the wire (Question A option 1), substrate needs a
  new concept.

- **`revoked_at` timestamp on key revocation.** `KeyEntry.status` is a string
  (`active`/`deprecated`/`revoked`). `verify_key_status()` rejects revoked
  keys with no temporal nuance. An auditor verifying historical events can't
  answer "was this key valid when this event was signed?" This matters for
  Question C (recovery) — revocation without a timestamp means you can't do
  temporal trust ("events before revocation are valid").

- **Multiple active signing keys.** `KeySet.active_key()` returns exactly one
  active key. There's no concept of "signing key A" vs. "recovery key B"
  for the same identity. This constrains Question C's "second-device recovery
  key" option.

### The concrete gap

```
BC-196 (asymmetric signing) is the floor. Without it:
  - Auditor model: unworkable (auditor needs signing secret)
  - Identifier format on wire: no fingerprint to use (only opaque key_id)
  - Recovery via second device: no keypair concept

BC-197 (delegation chain) is the ceiling. Already implemented:
  - on_behalf_of is schema-visible and HMAC-protected
  - DelegationChain dataclass exists but is unused
  - Missing: cryptographic enforcement (self-attested, same as actor_metadata)
```

---

## 3. Position on Question A: Identifier format on the wire

**Position: Opaque ID + attestation-type enum (option 2).**

### Code-grounded reasoning

Substrate's `actor_id` is already an opaque string (validated by
`_contract.py:499` for length ≤ 255, printable, no whitespace). It carries
no structure. The `key_id` on events is a separate opaque string from the
JSON key file. These two fields together already form a (who, which-key)
pair.

Option 1 (raw key fingerprint) is ruled out by the state of the code: there
are no asymmetric keys, so there are no fingerprints. BC-196 would need to
land first, which means this option blocks on a dependency that doesn't
have a timeline. We shouldn't gate the identity schema on something we
can't ship yet.

Option 3 (DID URI) would force substrate to add DID resolution to the
verification path. `verify_event` (`_signing.py:72`) would need to resolve
`did:key:z6Mku...` to raw verification material. That's a new dependency
with a spec surface that's larger than the rest of substrate combined. It's
the most invasive option by a wide margin.

Option 2 (opaque ID + attestation-type enum) is the least invasive:

```
What changes in substrate:
  - _types.py: Add id field and attestation_type: str | None to DelegationChain
    (the dataclass already exists, just unused; this is a field addition)
  - _contract.py: validate_delegation_chain() already validates on_behalf_of;
    add validation for attestation_type ∈ {"key", "oidc", "did", "none"}
  - Nothing changes in _signing.py (HMAC still covers the envelope)
  - Nothing changes in _keys.py (key_id stays as-is)
```

When BC-196 lands and Ed25519 is available, the attestation_type `"key"`
maps to verifying an Ed25519 signature with the public key referenced by
`id`. The `id` field can be a fingerprint at that point, but it doesn't
have to be one today. The protocol layer sees `id` as opaque and dispatches
on `attestation_type`. The deployment chooses what backs it.

This matches what `_contract.py:582` already does for `on_behalf_of` —
validate structure, defer semantic meaning to the deployment.

---

## 4. Position on Question B: Auditor access model

**Position: Durable audit bundles, with direct-read as a convenience path
for the solo scenario only.**

### What the workplace-audit use case actually requires

`agent-provenance/README.md` §8 says the driving scenario is "dogfood at
the owner's workplace... with internal compliance sign-off documentation."
This is internal IT audit — the auditor is a colleague, not the SEC.

But the document is careful to build toward external-audit readiness. §4
lays out a 6-layer trust model (HMAC → delegation → trusted timestamp →
asymmetric → witness → OpenTimestamps). §2 is explicit: "tamper-evident,
externally verifiable record... with offline verification by a third-party
auditor." The architecture sketch (§6) diagrams the verifier as an "offline
tool" with its own box in the flow.

The structural analog cited is AWS CloudTrail log-file integrity
validation. CloudTrail's model is: generate signed log files, deliver them
to an S3 bucket the customer controls, let the customer verify offline.
CloudTrail does NOT grant the auditor read access to the CloudTrail
internal database. That's the pattern agent-provenance should follow.

### Why bundles, not direct-read

1. **Segregation of duties.** An internal auditor with live DB read access
   is one misconfiguration away from write access. Bundles are pull-based
   and the auditor never touches the operational system.

2. **Archival.** Compliance sign-off happens at a point in time against a
   specific log segment. That segment needs to be self-contained and
   verifiable five years later, even if the operational database has been
   retired. A signed JSON bundle is an artifact you can store in the
   compliance document management system. Live DB access isn't.

3. **Transport.** Bundles travel over email, shared drives, or artifact
   repos. Direct-read requires network access, auth tokens, and operational
   coordination. For the solo-dogfood case, direct-read is convenient. For
   any scenario with an auditor who isn't also the operator, bundles win.

4. **Completeness attestation.** A bundle boundary is itself a claim:
   "these events constitute the complete log for period T." The bundle
   creator signs that claim. An auditor verifies the claim AND the events.
   Direct-read has no such boundary — the auditor never knows if they saw
   everything.

### The hybrid case is unnecessary

The consensus position says "direct verification for solo + auditor
scenario; bundles for compliance / arms-length scenarios." I argue this
doubles the implementation surface for marginal benefit. In the solo
scenario, the auditor IS the auditor — they don't need a separate direct-
read path. They generate a bundle (to themselves) and verify it. The
bundle path exercises the same code as the enterprise path. Two code paths
would mean the enterprise path is rarely tested.

**Recommendation:** Ship bundles as the primary model. Direct-read is a
local development convenience (the same person generates and verifies),
not a separate auditor access model. The verification CLI should work on
both a bundle file and a live substrate connection, but the bundle format
is the authoritative verification artifact.

---

## 5. Position on Question C: Recovery mechanism

**Position: OIDC-only for enterprise recovery; no recovery for
self-sovereign users.**

### v1 constraints analysis

v1 ships with:

- HMAC only (no asymmetric keys, no BC-196)
- `KeyEntry(status: str)` — active/deprecated/revoked, no `revoked_at` timestamp
- One active signing key
- No `DelegationChain` consumer
- No concept of key pairs or public keys

For each candidate:

**Second-device recovery key: RETROFIT NEEDED.**

This requires substrate to distinguish "signing key" from "recovery key"
in the same identity namespace. `KeySet` (`_keys.py:23`) would need a
key-purpose concept (signing vs. recovery). The signing envelope would
need to carry which purpose signed. `activate_key()` would need to
accept purpose. This is a non-trivial schema change to the key model and
is blocked on BC-196 (need asymmetric keys before you can have a recovery
keypair). **Does not fit v1. Would constrain v1 to add a key-purpose field
that has no consumer until v2.**

**Social recovery: RETROFIT NEEDED.**

Requires a multi-signature verification path that doesn't exist.
`verify_event` (`_signing.py:72`) verifies one signature against one key.
N-of-M co-signing would require a new event type (key_replacement with N
signatures), new storage, and a new verification path. The co-signers
themselves need identities, which means the identity system must be
bootstrapped before recovery can be added to it. **Maximum retrofit
surface. Would constrain v1 to reserve event types and schema fields for
multi-signature recovery that won't be used for multiple versions.**

**OIDC-only: NO RETROFIT NEEDED.**

Recovery is entirely a deployment concern. v1 substrate doesn't know about
OIDC. When BC-196 lands and asymmetric keys exist, an enterprise deployment
can bind `key_id` to an OIDC subject via a local configuration. Key
replacement then means: the OIDC provider re-authenticates the user, a new
keypair is bound to the same OIDC subject, and a signed rotation event
links old key to new key. Substrate sees only the rotation event (which it
already supports via key lifecycle states). **Zero v1 schema changes. The
`on_behalf_of` field is already structured to carry `authenticated_at` and
can carry a `binding` claim when needed.**

**No recovery, ever: NO RETROFIT NEEDED, BUT...**

This forecloses the question permanently. It's a policy decision, not a
technical one. The v1 code doesn't change. But the design doc's consensus
already says "recovery is a v2+ concern" — committing to "never" goes
further than that and would need to be reversed if a deployment with OIDC
wants it.

**Recommendation:** OIDC-only for enterprise recovery, documented as "no
recovery for self-sovereign users." This is the path of least constraint:
nothing changes in v1 substrate, the `on_behalf_of` and delegation schemas
are already flexible enough to carry OIDC binding claims, and the decision
doesn't foreclose adding social recovery or second-device recovery in a
future version if demand materializes. The self-sovereign case is
documented honestly: "key loss equals identity loss."

---

## 6. Disagreements with consensus

None. The nine consensus positions are well-reasoned and match the code
reality. I note only one **tension** that the consensus doesn't resolve:

The consensus says "opaque ID + attestation type" for the identifier format
and "peer model" for deployment. These are in tension: if every deployment
picks its own attestation type and its own opaque ID scheme, an auditor
receiving bundles from multiple sources has to implement every type.
There's no convergence story.

This isn't a disagreement — it's a real tension that the consensus
acknowledges without resolving. My read is: the tension is acceptable for
v1 because v1 has one deployment (the project owner's workplace). Cross-
deployment interoperability is a v2 problem that BC-196 (asymmetric
signing) and BC-198 (transparency-log anchoring) will address together.

---

## 7. Things the design doc misses entirely

### 7a. Session identity and lifecycle

The entire identity discussion is about *principals* (users, agents,
auditors). There is no concept of *session identity* — the fact that the
same principal can have multiple concurrent sessions, each with its own
scope, lifecycle, and trust boundary.

This matters because:

- A wake event targets a specific session (opencode's `session.prompt`
  takes a session `path`, Claude Code channels are launched per-session
  with `--channels server:NAME`).
- Provenance events need a `session_id` to correlate tool calls back to
  the session that executed them. The `DelegationChain` dataclass has
  `session_id` but it's unused.
- Session termination is a security boundary: when a session ends, wake
  events targeting it should be rejected (no injection into a closed
  session).

**This should be a design question.** The current docs conflate "who are
you" with "which session is this." They're different. A user (principal)
has many sessions. A wake event targets a session. A provenance event is
scoped to a session. The routing question from AGENTS.md ("how does an
event find the right session?") is unaddressed by the identity design.

Concrete proposal: add `session_id` as a first-class field in the wake
event schema, not just in the delegation chain. The harness adapter
resolves `session_id` to a native session handle. Agent-provenance records
`session_id` on every tool-call event. The session lifecycle (created,
active, terminated) is a substrate-level concept.

### 7b. Harness trust boundary mapping

The design assumes a uniform trust boundary: identity is stamped
out-of-band by the provenance recorder, the agent sees a plaintext copy,
prompt injection can corrupt the copy but not the provenance record.

But the two harness targets have different trust boundaries:

- **Claude Code channels** run as a subprocess launched with
  `--dangerously-load-development-channels`. The plugin is a separate MCP
  server process. The signing key can live in the plugin process; the
  agent's context window is in a different process.
- **opencode plugins** run *in-process* in the agent harness. The signing
  key would live in the same process as the agent's context window. This
  is a weaker boundary — a prompt injection that escapes the context
  window could reach the signing key.

The design should address whether the trust model is different for
in-process vs. subprocess plugins, and if so, how.

### 7c. Event schema evolution

What happens when the identity schema changes? v1 events might carry
`{ id, attestation_type }` as an opaque structure. v2 might add
`binding`. An auditor verifying v1 events with a v2 verifier needs to
know that the missing field is expected. Substrate's `verify_event` has
one backward-compat path (retry without `on_behalf_of`). As the
delegation chain grows, the backward-compat paths multiply.

This should be an explicit design question: does the event schema carry
a version field? Or does the verifier handle missing fields as "this
event predates that field"? Substrate's current approach (retry paths
in `verify_event`) is implicit and won't scale.
