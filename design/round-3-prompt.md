# Round 3 review prompt — final round

Copy from the horizontal rule below to share with the next round of
reviewers. This is the final review round before implementation
planning begins. The prior two rounds produced strong convergence on
the original nine questions; round 2 also surfaced several questions
the design doc had missed entirely. Round 3 focuses on those new
questions, reopens one settled question that round 2 challenged, and
gives reviewers a last opportunity to push back on anything else.

---

You are a reviewer in the **final round** of design review before
implementation planning begins for `agent-wake` and the shared identity
primitive that `agent-provenance` will depend on. Two prior rounds have
narrowed the design space substantially. This round closes it.

## What this round is for

Round 1 produced consensus on nine questions. Round 2 challenged that
consensus on two of them, confirmed the other seven, settled three
formerly-open questions, and surfaced several new questions the design
doc had missed entirely.

This round has three jobs:

1. **Primary: answer the new questions surfaced in round 2.** These
   were not in the original design doc and have not yet been debated.
2. **Secondary: re-examine the one settled question that round 2
   weakened.** Q6 (auditors as identities) is reopened.
3. **Last opportunity: push back on anything else.** If you think a
   "settled" answer is wrong, this is your last chance to argue it
   before code starts being written. The bar is high — the consensus
   is two rounds deep — but a sharp objection still wins.

## Mandatory pre-reading

You must read these before forming positions. Citations to specific
files, functions, and line numbers are required in your response.

### The design history

1. `/projects/agent-wake/design/identity-and-multi-user.md` — original
   design doc with the nine questions and the round-1 appended
   positions.

2. The round-2 reviews (read all of them — convergence and
   disagreement across reviewers is part of the input):
   - `/projects/agent-wake/design/round-2-response.md` (Claude)
   - `/projects/agent-wake/design/identity-review-round2-mimo.md`
   - `/projects/agent-wake/design/identity-and-multi-user-review-deepseek-v4-pro.md`
   - `/projects/agent-wake/design/review-kimi-k2.6-turbo.md`
   - `/projects/agent-wake/design/review-qwen3.6-27b-identity-multi-user.md`
   - `/projects/agent-wake/design/reviews/identity-and-multi-user-gemini-3.5-flash.md`
   - `/projects/agent-wake/design/reviews/identity-and-multi-user-glm-5.1.md`

### The substrate code

3. `/projects/substrate/src/substrate/_signing.py` — signing envelope,
   `on_behalf_of` field, `build_signing_envelope()`, `verify_event()`.
4. `/projects/substrate/src/substrate/_keys.py` — `KeyEntry`,
   `KeySet`, lifecycle states, hot-reload.
5. `/projects/substrate/src/substrate/_types.py` — `DelegationChain`
   dataclass (unused), `ActorKind` enum, `Event` schema.
6. `/projects/substrate/src/substrate/_event_store.py` — note where
   the timestamp is set relative to signing.
7. `/projects/substrate/breadcrumbs/resolved/196-*.md` —
   asymmetric signing requirements.
8. `/projects/substrate/breadcrumbs/resolved/197-*.md` — delegation
   chain (already implemented).
9. `/projects/substrate/breadcrumbs/resolved/198-*.md` — operator
   forgery defense, RFC 3161 timestamping.

### The consumer

10. `/projects/agent-provenance/README.md` — driving use case,
    workplace audit, scope attestation requirement (§2).

**Your output will be discounted if it does not cite specific files
and functions. Output that only re-states positions from prior rounds
will be discounted. The bar is "new substantive contribution,"
positive or negative.**

## What is now locked (with last-bite-at-the-apple invitation)

After two rounds of review, the following positions are taken as the
working consensus. They are locked unless you produce a sharp
objection grounded in code or use-case evidence. State the locked
position only to disagree with it; do not restate it to agree.

### Locked

- **Q1.** Identity primitive lives in substrate. The mapping from
  identifier to real-world human is a deployment concern, not a
  substrate concern.
- **Q2/A.** On-the-wire identifier is opaque + attestation-type
  (or "method") discriminator. Specific naming and exact field shape
  are still implementation choices, but raw fingerprint and DID URI
  are both rejected. **Round-2 refinement:** URI-prefix encoding
  (`key:sha256:...`, `oidc:...`, `did:...`) is a candidate
  implementation that requires zero substrate schema change, since
  `principal_id` at `_contract.py:590` is already a bare string.
- **Q3.** Peer model first. Hosted is a different project.
- **Q4.** Trigger and actor identity are both first-class fields.
  Identity is stamped out-of-band by the provenance recorder, not
  passed through the agent context. In-context identity metadata
  is advisory only.
- **Q5.** Strict attribution for v1. Every tool call attributed to
  the harness operator. Delegation chain (`on_behalf_of`) is
  schema-present but cryptographically self-attested until BC-196
  lands.
- **Q7.** Local keygen, signed rotation events, signed revocation
  events. **Round-2 refinement:** revocation must carry a `revoked_at`
  timestamp (gap in current `_keys.py`). Recovery is a v2 feature but
  the v1 schema must hold a hook for it.
- **Q8.** Per-user sender allowlists. Cross-user consent is a signed
  event in the log.
- **Q9.** Single-user is the degenerate case of multi-user. No
  separate code path.
- **Question B (round 2).** Hybrid auditor access — direct
  verification for discovery, durable bundles for evidence. The
  framing is "auditor selects from the live log during discovery,
  takes away a signed Merkle bundle with RFC 3161 timestamp anchoring
  for evidence."
- **Question C (round 2).** Second-device recovery key. V1 ships
  with the schema hook (either `role: "primary" | "recovery"` on
  `KeyEntry` or `recovery_pubkey` as a separate optional field).
  Implementation is v2.

### Reopened

- **Q6 (auditors are identities too).** Round-2 reviewers split. The
  round-1 consensus said auditors are just another identity with the
  same primitive. Round-2 reviewers argued the threat profile differs
  (auditor key compromise corrupts the verification layer itself),
  and that overloading the actor-identity primitive with access
  control is a category error. The cleanest dissent (Kimi):
  *actor identity = signing key for log events; auditor access =
  separate token / public-key allowlist; auditor attestation = signed
  artifact, but stored as a bundle co-signature, not an event in the
  live log*. Several reviewers proposed a `role` or `capability`
  field on `KeyEntry` instead of a separate primitive — a middle
  path.

Your job on Q6: pick one of (a) the original consensus, (b) the
Kimi/GLM dissent, or (c) the middle path with `role`/`capability`
on `KeyEntry`, and argue it grounded in the substrate code.

## What is open (primary focus of this round)

These questions were missed by the original design doc and surfaced
by round-2 reviewers. They have not yet been debated at depth.

### New Q10. Scope attestation

`agent-provenance/README.md` §2 explicitly requires the scope
statement — "this substrate instance captures events from harnesses
A, B, C; other sources are out of scope" — to be a signed first-class
event in the log. Auditors verify completeness (no missing events)
against this attestation, not just integrity (events that exist are
valid).

The design doc's nine questions don't address this. Round-2
reviewers (5/7) flagged it as a structural gap.

Sub-questions to answer:

- **Who signs the scope statement?** The operator? A bootstrap event
  signed at substrate init? A periodic attestation from a privileged
  identity?
- **Is scope versioned?** If a harness is added mid-period, events
  from before the addition were captured under a narrower scope.
  Auditors need point-in-time scope — versioned attestation. How is
  this represented?
- **Does scope evolution require a new attestation per change, or a
  scope-rotation event analogous to key rotation?**
- **Gemini proposed harness heartbeats** — periodic signed
  "keep-alive" events from configured harnesses, so an auditor's
  offline verifier can detect gaps where the operator routed around
  the harness. Is this in scope for v1?

### New Q11. Agent-key vs. human-key topology

This is the strongest single observation from round 2 (Kimi).

The current design has the agent and the human sharing one signing
key (or assumes a single `actor_id` per event with no cryptographic
separation between the human who authorized the action and the agent
that executed it). Under HMAC, this means the delegation chain
(`on_behalf_of`) is structurally present but cryptographically
meaningless — anyone with the HMAC key can forge any
`principal_id` they want. With asymmetric signing (BC-196), the same
problem persists unless agent and human hold separate keys.

The proposed alternative topology:

- Human holds a long-lived identity keypair (in `~/.config/`).
- Agent harness holds an ephemeral session keypair (generated per
  session or per harness start).
- Human signs a **session grant** (delegation certificate) that says
  "public key `0xAgent...` may sign events on my behalf for the
  next N hours, with scope X."
- Agent signs tool-call events with its session key. Verifier checks
  the event signature against the agent key, then checks the session
  grant signature against the human key. Two cryptographic links,
  two separate compromise blast radii.

Sub-questions to answer:

- **Should v1 support this topology, or just design the schema to
  permit it later?** `_signing.py:sign_event()` currently takes one
  `key: bytes`; this topology requires the event chain to verify
  against two keys (agent's key for the event, human's key for the
  grant).
- **What's the lifetime of a session grant?** Time-bounded? Scope-bounded?
  Revocable mid-session?
- **How does this interact with the Q6 auditor question?** An
  auditor's attestation would presumably be signed with the human
  identity key directly, not an ephemeral agent key. Does this argue
  for the Q6 dissent (auditor primitive is different from actor
  primitive)?
- **What about Claude's session-identity observation (round 2,
  section 7a)?** Session is currently conflated with principal.
  Wake events target sessions; provenance is scoped to sessions. The
  session grant mechanism would naturally produce a `session_id` — is
  this how those tie together?
- **Code constraint**: `_signing.py:sign_event()` returns one
  signature. Two-key verification means either (a) the session grant
  is a separate signed event referenced by every action event, or
  (b) the event envelope carries both signatures. Which?

### New Q12. Temporal integrity

The signing envelope (`_signing.py:build_signing_envelope`) does
**not** include the event timestamp. The timestamp is set at
`_event_store.py:88` (`datetime.now(UTC)`) *after* signing. An
operator with the HMAC key can therefore create events with
arbitrary timestamps without breaking signature verification.

Multiple reviewers (5/7) flagged this. BC-198 tracks it.
Deepseek and GLM both noted that BC-198 Layer 1 (RFC 3161
timestamp tokens from an external TSA) is implementable today
against the existing HMAC primitive — it does not wait for BC-196.

Sub-questions to answer:

- **Should `timestamp` be added to the signing envelope?** This is a
  one-line change to `build_signing_envelope()` but breaks
  backward-compat with all existing signed events. Is that
  acceptable now, while there's no production deployment?
- **What's the relationship between three different timestamps?**
  `authenticated_at` (in `on_behalf_of`, set by caller), `timestamp`
  (in event header, set by event store), and RFC 3161 token
  timestamp (from external TSA). Reviewers proposed an ordering
  constraint: `authenticated_at ≤ event.timestamp ≤ TSA timestamp +
  tolerance`. Should this be enforced? By what?
- **Who provides the TSA?** For SOX, the auditor may require a TSA
  they trust, not one the auditee picks. Is the TSA configurable
  per-deployment? Does v1 ship with a default?
- **Batch interval for TSA tokens.** Per-event is too expensive;
  hourly may be too coarse for some audits. What's the v1 default
  and is it configurable?

### New Q13. Trust-root bootstrap

The peer model says users exchange public keys out-of-band. But when
an auditor receives a bundle and needs the actor's public key to
verify it, asking the operator is circular — the operator could
supply a fraudulent public key and a bundle signed by the
corresponding private key, and the auditor would verify successfully.

The auditor needs the actor's public key through a channel the
operator does not control.

Sub-questions to answer:

- **What's the v1 bootstrap mechanism?** Candidates from reviewers:
  DNS TXT records, GitHub SSH-key directories, enterprise SSO
  certificates, a separate signed key-registry artifact, manual
  key-exchange ceremony.
- **For the solo-dogfood case (auditor and operator are the same
  person), is bootstrap a no-op?** And if so, what's the
  documentation contract that says "this works for dogfood but is
  inadequate for arms-length audit"?
- **Is the bootstrap mechanism itself a signed first-class event?**
  An auditor needs to know "the operator declared their public key
  to be X at time T" — without that, key rotation history is
  unverifiable for new auditors joining later.

### New Q14. Identity continuity across key rotation

If `principal_id` is structured as the consensus suggests (opaque ID
+ attestation-type), and the underlying key rotates, does the
opaque ID stay stable or change?

GLM-5.1 raised this as the one honest argument for DIDs that no
prior reviewer engaged with. DIDs have a built-in rotation/history
mechanism. The consensus's opaque-ID approach defers this.

Sub-questions to answer:

- **Is the opaque ID a stable human-readable name (`"alice"`) that
  binds to a sequence of keys, or is it the fingerprint of the
  current key (which means it changes on rotation)?**
- **If stable, where is the binding stored?** In substrate's key
  registry? In an external IdP? In a signed rotation event chain
  in the log?
- **What does an auditor see when verifying historical events
  signed by alice-key-v1 against the current key alice-key-v2?**
  The verification must follow the rotation chain. Is this the
  verifier tool's responsibility or substrate's?
- **Does this push us back toward DIDs?** The objection in round 2
  was "DIDs add a discovery layer substrate has no code for." But
  if we're building a rotation chain mechanism anyway, the DID
  ontology is at least battle-tested. Worth re-examining?

## What we'd like you to consider but is lower priority

These are real concerns raised in round 2 that may be addressable as
implementation details rather than design questions. Note any sharp
objections but don't spend disproportionate effort.

- **`alg` discriminator forward-compatibility.** When BC-196 lands,
  consumers need a fail-shut rule for unknown algorithm values. Most
  reviewers agree this is settled (fail-shut), but the exact contract
  hasn't been written.
- **Substrate-less deployment.** `agent-wake/AGENTS.md` says HTTP
  ingest should work without substrate. If identity lives in
  substrate, what signs in substrate-less mode? GLM-5.1 raised this
  as a real gap.
- **GDPR / right-to-erasure collision.** Append-only logs containing
  personal data conflict with Article 17. Kimi raised this as a
  question that should be asked before v1 schema freezes. A possible
  answer is redaction-by-hash (the field's hash is signed; the
  cleartext lives in a separate erasable store), but the contract
  needs design.

## Required output format

Your response must include:

1. **Pre-reading attestation.** List the files you read, one line
   each, with the most surprising thing you found. Include the
   prior reviews — what stood out across reviewers is itself input.

2. **Substrate code findings beyond what prior reviewers caught.**
   By now, prior reviews have catalogued most of what substrate
   does and doesn't support. New findings welcome but expected to
   be marginal.

3. **Q6 (reopened): auditors.** Pick (a), (b), or (c) from above
   and argue from code.

4. **New Q10: scope attestation.** Concrete proposal.

5. **New Q11: agent-key vs. human-key topology.** This is the
   highest-priority new question. Engage with it carefully.

6. **New Q12: temporal integrity.** Concrete proposal including
   answers to the sub-questions.

7. **New Q13: trust-root bootstrap.** Concrete proposal.

8. **New Q14: identity continuity across rotation.** Position.

9. **Last bite at the apple (optional).** If you think any of the
   eleven locked positions is wrong, this is your last chance. The
   bar is high — produce evidence, not opinion.

10. **Anything still missed.** If you find a question the design
    doc and prior reviewers all missed, raise it.

## Format constraints

- Markdown only. **No LaTeX equations** — they don't render in our
  viewers. Two reviewers in round 2 ignored this; please don't.
- Mermaid diagrams allowed; ASCII preferred.
- File-and-function citations in backticks or code fences.
- Length budget: thorough but not exhaustive. If you find yourself
  restating prior reviewers' positions, stop and cut.
- This is the final round before implementation. Write for a reader
  who will use your output to make implementation decisions, not
  for a reader looking for further debate.
