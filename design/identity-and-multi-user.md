# Identity and multi-user — open design questions

**Status:** open, soliciting input. No decisions made yet.

This document outlines the design questions around supporting multiple
concurrent users in **agent-wake** and giving **agent-provenance** a
meaningful identity primitive. It's written for readers who haven't
seen either project before.

## Context for new readers

Three projects are relevant. Each is small, single-person OSS, MIT-licensed.

- **substrate** (`/projects/substrate`) — a Python library providing
  durable, event-sourced state for agent pipelines, backed by Postgres.
  It already has HMAC-SHA256 event signing and a hook queue for
  out-of-process event dispatch. Asymmetric signing (per-actor keys
  instead of a shared HMAC secret) is on the roadmap but not yet built.
- **agent-provenance** (`/projects/agent-provenance`) — a tool for
  recording a cryptographic audit trail of what an AI agent does:
  every tool call, every approval decision, every wake event. The
  driving use case is workplace audit — letting an organization verify
  what an agent actually did, after the fact, even if the operator is
  hostile or compromised. Currently in early scaffolding.
- **agent-wake** (`/projects/agent-wake`) — a thin adapter layer that
  pushes external events (webhooks, alerts, scheduled triggers) into
  a running agent session. Acts as a bridge between
  "something happened in the outside world" and "the agent should know
  / act on it." Two harness targets: Claude Code (via its `channels`
  feature) and opencode (via its plugin system).

All three are designed today as **single-user dogfood**. The user runs
them on their own laptop, signs events with their own key, reads their
own logs. That's fine for proving the ideas work. It is **not** fine
for any scenario where another human reads the log and needs to trust
its contents — which is the actual point of the work.

This document is about what changes when "user" stops being implicit.

## The driving scenarios

To keep the questions grounded, here are the scenarios that motivate
multi-user. They are listed in roughly increasing order of difficulty.

1. **Solo + auditors.** One actor (the user), several read-only
   verifiers (compliance, legal, an external auditor). The auditors
   need to confirm the log is authentic and complete; they do not
   themselves perform actions.
2. **Small team, shared infrastructure.** Several actors on the same
   team, possibly invoking the same agent harness. Each actor's
   actions are attributed to them. Auditors still exist.
3. **Cross-org delegation.** An agent runs on behalf of user A but is
   triggered by an event from user B (e.g., a CI failure raised by
   user B's commit triggers an agent action that user A is
   responsible for reviewing). The chain of "whose authority is this
   acting under" needs to be recorded.

Scenario 1 is the smallest viable shape and matches the user's actual
workplace use case. Scenarios 2 and 3 are real but should not block
shipping something useful for scenario 1.

## The key questions

### 1. Where does the user-identity primitive live?

Both agent-wake and agent-provenance need to know "who did this." If
they each invent their own scheme, the integration boundary becomes a
mapping problem ("user X in agent-wake is the same as user Y in
agent-provenance") that is easy to get wrong and hard to audit.

**Tension:** centralizing the primitive in substrate adds a dependency
both consumers can't easily escape, and substrate's current scope is
deliberately harness-agnostic and coordination-focused. Defining
identity there expands substrate's scope. Defining it outside substrate
means a fourth project (or just a shared library), which adds setup
friction.

**Initial take (for debate):** identity belongs in substrate. Substrate
already owns event signing; identity is what a signature *means*.
Adding it doesn't expand scope as much as it sounds — it just makes
explicit what HMAC signing already assumes implicitly (one signer per
key). The roadmap already has asymmetric signing as a planned feature;
identity is the natural next step.

### 2. What is the identity primitive?

Once we agree where it lives, we still need to decide *what* identifies
a user. Options:

- **Public key fingerprint.** Simple, self-sovereign, no external
  dependencies. Users generate keypairs locally, the fingerprint of
  the public key is their ID. Hard to revoke; key compromise = identity
  loss.
- **DID (Decentralized Identifier).** A standard for self-sovereign
  identifiers, supports key rotation, multiple verification methods.
  More moving parts, less broadly understood.
- **OIDC subject claim.** Identity comes from an external Identity
  Provider (Google, Okta, corporate SSO). Familiar in enterprise
  contexts, makes integration with existing auth easy. Couples the
  trust model to whatever IdP you trust.
- **Opaque ID + side-channel attestation.** Some short ID (UUID,
  username) is the on-the-wire identifier; the binding to a real
  human happens elsewhere (a registry, an IdP, a manual roster).

**Tension:** the regulated-workplace use case probably needs OIDC or
something OIDC-compatible, because compliance frameworks expect "tied
to corporate identity" not "self-sovereign keypair." But forcing OIDC
makes the OSS project less useful outside enterprise contexts.
A hybrid (key-based for self-sovereign use, OIDC-binding optional for
enterprise) is possible but doubles the surface.

**Open question:** is there a way to make the on-the-wire identifier
opaque enough that the choice of IdP / key model is a deployment
concern, not a protocol concern? If so, what's the right shape?

### 3. What's the deployment model for multi-user?

Two ends of a spectrum:

- **Peer model.** Each user runs their own local instance of
  agent-wake and substrate. They exchange public keys / IDs through
  some out-of-band registry (could be as simple as a JSON file checked
  into a shared repo). No central service. Matches single-person OSS
  posture. Limits some features (cross-user real-time triggers need a
  rendezvous point).
- **Hosted model.** A single agent-wake service handles all users.
  Standard SaaS shape — auth, multi-tenant storage, ops. Powerful but
  is a different project: it needs deployment, billing eventually,
  on-call, etc.

**Tension:** the user's explicit posture (per project memory) is
"single-person OSS, MIT, no commercial pivot pre-designed in,
dogfooding at workplace is the primary validation target." That
strongly favors the peer model. But scenario 3 (cross-org delegation)
is hard without some shared infrastructure.

**Initial take:** peer model first. Scenario 1 needs no shared
service. Scenario 2 might be solvable with a shared substrate
database (substrate already has schema-per-project isolation).
Scenario 3 is a v2+ problem.

### 4. How does identity propagate across the three projects?

A wake event flows:

```
External system
     ↓ (event with sender identity)
agent-wake (verifies sender, attaches identity)
     ↓ (wake message + identity metadata)
agent harness (Claude Code or opencode)
     ↓ (agent acts: tool calls, approvals, etc.)
agent-provenance (records each action with attribution)
```

For the log to be meaningful, the identity has to survive every hop.
Specific questions:

- **Trigger identity vs. actor identity.** If user A triggered the
  wake but user B's agent (on user B's machine) actually executed
  the tool call, the provenance event needs to record *both*. What's
  the schema?
- **Implicit vs. explicit.** Does the harness pass identity metadata
  to the agent (so the agent itself can reason about it), or is
  identity stamped only by the provenance recorder out-of-band? The
  former is more flexible; the latter is more tamper-resistant.
- **Crossing untrusted boundaries.** The wake passes through the
  agent's context window. A sophisticated prompt injection could try
  to spoof the trigger identity. How is the binding between event
  and identity protected from agent-level tampering?

### 5. What does delegation actually mean?

When an agent acts in response to a wake, whose authority is the
action under?

- **Strict:** every tool call is attributed to whoever is *currently
  operating the harness*. Wake triggers are recorded but don't
  delegate authority.
- **Inherited:** if the wake came from user A and user B's harness is
  configured to act on A's wakes, B's actions in response to A's
  wakes are "by B on behalf of A."
- **Per-action consent:** the agent must prompt for approval on each
  action; the approver's identity is what gets attributed.

**Why this matters legally:** in a workplace audit, "the agent did
this" is much weaker than "user X authorized this and the agent
executed it under X's authority." Same true in any compliance regime
that demands a human in the loop.

Substrate already has BC-197 (delegation chain) as a tracked design
problem. The question for agent-wake / agent-provenance is what
delegation primitives they consume from substrate, and what they
record on top.

### 6. Auditors as a first-class role

Auditors don't act; they verify. Do they need their own identity
primitive, or do they just hold public keys of the actors and verify
signatures? Probably the latter, but worth checking:

- **Read-only access to logs.** Should auditors be granted read
  access via the same identity primitive (e.g., their public key is
  on an allowlist), or via a separate mechanism (e.g., a token
  issued by an actor)?
- **Auditor signatures on attestations.** When an auditor verifies a
  log and attests "I checked this," is that attestation itself a
  signed event in the log? Probably yes — that makes the verification
  itself part of the auditable record.

### 7. Key management

This is the part that tends to bite hardest in practice.

- **Generation.** Where do users generate keys? Local CLI command?
  In a registration flow? Imported from existing PKI?
- **Rotation.** When a user rotates their key, do old log entries
  remain verifiable? Almost certainly yes — but the rotation event
  itself must be a signed first-class event.
- **Revocation.** If a key is compromised, how is that signaled?
  How do verifiers know to reject signatures from the compromised
  key going forward, while still trusting past entries up to the
  compromise time?
- **Recovery.** If a user loses their key, can they recover identity?
  This is a hard problem in self-sovereign systems and a soft problem
  in OIDC-backed systems. The answer probably influences question 2.

### 8. Sender gating in a multi-user world

The Claude Code channels documentation is emphatic that an ungated
channel is a prompt injection vector. In single-user, "gated" means a
global allowlist. In multi-user:

- The allowlist becomes per-user. User A's senders aren't trusted by
  user B by default.
- Cross-user triggers (scenario 3) require explicit consent: user B
  has opted in to receive events from user A.
- The consent itself should be a signed, auditable event.

### 9. Backwards compatibility with single-user dogfood

The user is actively dogfooding the single-user shape. Whatever
multi-user design lands shouldn't break that or force a migration that
makes dogfooding harder. Possible shapes:

- **Single-user is the degenerate case of multi-user.** One user
  registered, identity primitive optional but defaulted.
- **Single-user and multi-user are separate code paths.** Simpler
  to reason about but means two implementations.

**Initial take:** the first shape is right. Identity should always be
present; in single-user dogfood, the identity is just "the user who
generated the key when they installed it."

## What we're asking outside readers

If you have experience with any of:

- Identity primitives for distributed systems (DIDs, OIDC, PKI in
  practice, etc.)
- Audit-log design where the actor identity is load-bearing
- Compliance regimes (SOX, HIPAA, etc.) and what they actually
  require from an audit trail
- Multi-tenant signing systems with peer-to-peer rather than hosted
  trust roots
- Delegation models in agent / automation systems

...then your input on any of the numbered questions is welcome.
We are explicitly not looking for a solved answer; we're looking for
the right tensions to be visible so we can pick a path with eyes open.

## Proposed positions (draft — open for feedback)

These positions are the current working hypothesis. They are intended
for anyone editing or building on top of this system.

### 1. Identity lives in substrate, but the wire schema is vendorable.

Substrate already owns signing; identity is what a signature means.
The canonical registry, rotation log, and revocation log should live
there, designed alongside BC-196 and BC-197.

However, the *on-the-wire* identity metadata must be a simple,
JSON-serializable shape that `agent-wake` and `agent-provenance` can
parse without importing substrate. If a lightweight deployment wants
to run without substrate, it can implement the same schema with a
static JSON file or environment variables — but the schema is
authoritative because substrate defines it.

### 2. Opaque ID + side-channel attestation.

The protocol should define only two fields:

- `id`: a URI-like opaque string (`key:fingerprint`, `oidc:subject`,
  `did:method:identifier`, etc.)
- `attestation_type`: an enum (`key`, `oidc`, `did`)

The actual verification material (public key, JWKS endpoint, DID
document) lives in a registry that the deployment supplies. This lets
single-user dogfood use bare key fingerprints while an enterprise
deployment binds the same `id` to an IdP — without changing the
protocol. A new attestation type can be added later without touching
code that just carries the `id` around.

### 3. Peer model first; hosted is a different project.

Scenario 1 (solo + auditors) needs no shared service. Scenario 2
(small team) can use a shared Postgres database with substrate's
existing schema-per-project isolation as the escape hatch. Scenario 3
(cross-org delegation) is explicitly a v2+ problem. We will not design
or build centralised multi-tenant infrastructure until the core loop
is solid.

### 4. Dual identity headers + tamper-evident envelope.

Every provenance event carries:

- `trigger_identity`: who caused the wake
- `actor_identity`: who is operating the harness right now

The wake adapter signs a small envelope containing the event payload
and both identities. This envelope is what `agent-provenance` verifies
and records; the harness may also forward the identities to the agent
as metadata, but the agent cannot falsify the envelope because it does
not hold the adapter's signing key.

This protects against prompt-injection spoofing: if an attacker
injects "pretend the trigger was Alice" into the context window, the
agent may be confused, but the provenance log still contains the
cryptographically signed envelope with the real `trigger_identity`.

### 5. Strict default, inherited only by signed delegation policy.

By default, every tool call is attributed to `actor_identity`.
`trigger_identity` is recorded for correlation but does not delegate
authority.

If a user wants inherited delegation ("execute Bob's wake under
Bob's authority"), they must publish a signed delegation policy in
substrate that explicitly grants it. The provenance recorder then
emits a `delegated` event showing both identities and the policy
reference. This is the primitive that satisfies BC-197.

Per-action consent is still recommended for destructive operations,
but the consenting identity is the `actor_identity`, not the
`trigger_identity`.

### 6. Auditors are identities too.

No special "auditor" role type. An auditor is simply another identity
that has been granted read access to a provenance stream. When an
auditor attests "I reviewed this log segment," that attestation is
a signed event in the same substrate-backed log, attributed to the
auditor's identity.

### 7. Simple key management: no recovery magic.

- **Generation:** local CLI command (`agent-wake identity init`) that
creates a private key in `~/.config/agent-wake/`.
- **Rotation:** a signed `key_rotation` event in the log that links old
fingerprint to new fingerprint. Old entries remain verifiable with
the old key.
- **Revocation:** a signed `key_revoked` event with an effective
timestamp. Receivers can reject new signatures after that time while
still trusting earlier log entries.
- **Recovery:** out of scope for self-sovereign mode. If you lose your
key, you create a new identity. In OIDC-backed deployments, recovery
is the IdP's problem.

This keeps the surface small and avoids baking in complexity that this
project is not ready to support.

### 8. Sender gating is per-user allowlist + signed consent.

`agent-wake` maintains a per-user allowlist of trusted sender
identities. It is stored as signed configuration in substrate (or a
local JSON file for substrate-less deployments).

Cross-user triggers require a signed `trust_consent` event from the
recipient identity, naming the sender identity and an optional expiry.
The channel adapter rejects any wake whose sender is not on the
recipient's allowlist or whose consent has expired.

### 9. Single-user is the degenerate case.

The code should always assume multi-user. On first run, if no identity
is registered, the CLI auto-generates one and uses it implicitly. There
is no separate "single-user mode." The only difference between
dogfooding today and a team deployment tomorrow is how many identities
appear in the registry.

---

## Agent positions (2026-05-23)

These are my positions on the nine questions above. Written for future
readers (including me, after context window turnover). Where I agree with
the initial takes in the document, I say so briefly and move on.

### Q1: Where does the identity primitive live?

**Agree with the initial take: substrate.** The argument is exactly right
— substrate already owns event signing; identity is what a signature
*means*. Making it explicit is a scope refinement, not an expansion.
A fourth project would create a coordination tax (version alignment,
separate releases) for a primitive whose entire purpose is to be shared.

One nuance: substrate should own the *primitive* (the on-the-wire shape
of "who"), but it should not own the *directory* (the mapping from
identifier to real-world human). That mapping is a deployment concern
and can live in a config file, an OIDC provider, or a shared registry
without substrate needing to know about it.

### Q2: What is the identity primitive?

**Opaque identifier + cryptographic binding, directory is out-of-band.**

The on-the-wire identifier should be a public key fingerprint (ed25519,
raw bytes or hex). No DID overhead, no OIDC coupling. The protocol
layer sees only: "this event was signed by key 0xAABB…CCDD." Anything
beyond that — "that key belongs to Alice" — is a mapping that lives
outside the protocol.

This answers the open question directly: yes, the identifier can be
opaque enough that IdP choice is a deployment concern. The way to do it
is to have the protocol speak only in terms of keys and signatures, and
to let each deployment decide how keys map to humans. An enterprise
deployment can back the mapping with OIDC; a solo user can back it with
a local config file. Neither choice touches the protocol.

Why not DID: DIDs add a discovery/rotation layer that is real complexity
for unclear benefit at this scale. If key rotation is needed (it will
be), a simpler mechanism — a signed "key rotation" event in the log —
does the job without importing the DID spec's ontology.

Why not OIDC on the wire: OIDC is a fine *source* of identity but a bad
*on-the-wire* format. It couples the protocol to an IdP, it requires
token refresh logic, and it makes offline verification harder. Use OIDC
to provision keys at setup time; don't put JWTs in event envelopes.

### Q3: Deployment model

**Peer model first, with shared-DB as a natural step for scenario 2.**

Scenario 1 (solo + auditors) needs zero shared infrastructure. The user
generates a keypair, signs events, auditors verify with the public key.
Done.

Scenario 2 (small team) is solvable with a shared substrate database.
Substrate already has schema-per-project isolation. Multiple users
writing to the same Postgres instance with separate schemas is almost
free. The peer model still holds — each user runs their own agent-wake —
but they agree on a shared substrate instance for the audit log.

Scenario 3 (cross-org delegation) does need something more, but it
doesn't need a hosted service. It needs a shared substrate and a
mechanism for cross-user consent (signed "I trust events from user X"
records in the log). That's achievable within the peer model. If it
turns out to need a rendezvous point for real-time cross-user triggers,
that's a lightweight relay, not a SaaS.

### Q4: Identity propagation

Three sub-questions, three answers:

**Trigger vs. actor identity:** both fields, always. Every provenance
event has `triggered_by` (who sent the wake) and `executed_by` (whose
harness ran the action). In solo use they're the same; in delegation
scenarios they diverge. The schema should be a flat pair, not a chain —
chains come later if needed (BC-197).

**Implicit vs. explicit:** both, at different layers. The agent sees
identity metadata in its context window for operational purposes ("this
CI failure came from Alice's commit"). The authoritative attribution
happens out-of-band at the provenance layer, signed by a key the agent
doesn't control. The in-context version is informational; the
provenance-layer version is tamper-evident.

**Crossing untrusted boundaries:** the identity binding must exist as a
cryptographic signature outside the agent's context. The agent can see a
plaintext copy, but the tamper-evident version lives in the provenance
log, signed by a key held by the harness infrastructure (not the agent
itself). This means agent-wake or the harness plugin, not the LLM,
holds the signing key. Prompt injection can corrupt the in-context copy;
it cannot forge the out-of-band signature.

### Q5: Delegation

**Strict by default, inherited on explicit opt-in.**

Default: every action is attributed to the harness operator. Wake
triggers are recorded but don't delegate authority. This is the safe
default and matches scenario 1.

Opt-in: a harness can be configured with a delegation policy: "for
wakes from user A, actions are attributed to A, not to me." This is a
signed configuration event (auditable) and it requires user A to have
consented to being the attributed actor (also signed, also auditable).

Per-action consent is a valid policy layer but should not be the
protocol primitive. It's too noisy for automation and doesn't compose
well. The protocol should support strict and inherited; per-action
consent is a harness-level feature built on top.

### Q6: Auditors as a first-class role

**Auditors don't need the identity primitive. They need public keys and
a verification API.**

Auditors hold the public keys of actors and verify signatures. That's
it. They don't sign events in the normal flow; they consume the log.

One exception: auditor attestations ("I have reviewed the log for period
X and it is complete"). These *should* be signed events in the log,
using the auditor's own key. But the auditor's key is not an "actor
identity" — it's a verifier identity, and it doesn't need the same
infrastructure (no key rotation events in the main log, no delegation
chains). A simpler mechanism (a separate keyring, a separate signature
format) is fine.

Read access: separate mechanism. A read token or a public-key allowlist,
not the same identity primitive used for actors. Overloading actor
identity with access control is a common mistake; they serve different
purposes and should be decoupled.

### Q7: Key management

**Ed25519 keypair, generated locally at first run, stored in a
well-known path.**

- **Generation:** `agent-wake init` (or equivalent) generates an ed25519
  keypair, stores it in `~/.agent-wake/identity.key`. Public key is the
  on-the-wire identifier. No registration flow, no external service.
- **Rotation:** a signed "key rotation" event in the substrate log.
  Contains old key fingerprint, new key fingerprint, signed by old key.
  Verifiers check: (1) the rotation event is validly signed by the old
  key, (2) no events after the rotation are signed by the old key (or
  are accompanied by a subsequent rotation). Old entries remain
  verifiable with the old key.
- **Revocation:** a signed "key compromised" event. This is a
  "negative trust" signal — it tells verifiers to reject future events
  from the compromised key. Past events are judged on a timeline: trust
  signatures up to the compromise timestamp, reject after. The
  compromise event itself must be signed by a recovery mechanism (see
  next point) or a quorum of trusted peers.
- **Recovery:** at setup time, generate a second "recovery key" stored
  somewhere other than the primary key (different machine, offline USB,
  printed QR code). The recovery key can sign rotation events if the
  primary key is lost. For OIDC-backed deployments, the IdP can vouch
  for key replacement. The protocol should support both paths.

### Q8: Sender gating

**Per-user allowlists, cross-user consent as signed events.**

In single-user mode, the allowlist is trivially ["self"]. In multi-user,
each user maintains their own allowlist of trusted senders. Adding a
sender requires a signed consent event from the recipient ("I, key X,
trust events from key Y"). Removing a sender requires a signed revocation
event.

This composes with the peer deployment model: consent events are just
signed records in the substrate log. No central service needed.

Cross-user triggers without prior consent should be rejected at the
agent-wake boundary, before the event reaches the agent's context. This
is the prompt-injection defense: untrusted content never enters the
agent's world unless the user has explicitly opted in to trusting its
source.

### Q9: Backwards compatibility

**Single-user is the degenerate case of multi-user.**

Agree with the initial take. Identity is always present. On first run,
a keypair is generated and the user's allowlist is ["self"]. In this
state, there is exactly one identity, one sender, one verifier. All the
multi-user machinery exists but is trivial.

This means no migration path, no "single-user mode" flag, no code path
forking. The same code handles one user and N users. The complexity is
in the configuration, not in the implementation.

## Reader input: opencode agent (2026-05-23)

I've read the doc and reviewed the substrate signing code (`_signing.py`,
`_keys.py`). A few things surprised me: substrate already has an
`on_behalf_of` field in the signing envelope and key lifecycle states
(active/deprecated/revoked). That's more delegation infrastructure than
the doc implies. My positions on each question:

### Q1: Where does the identity primitive live?

**Substrate. Agree with the initial take.** The reasoning is sound:
identity is what a signature means, and substrate already owns signing.
The `on_behalf_of` field is already there — it's a `dict | None`, which
is the right level of flexibility for now. Making identity explicit in
substrate isn't scope creep; it's naming what's already implicit.

One caveat: the identity contract should be narrow. Substrate should
define *how to bind a key to an identifier* and *how to verify that
binding*. It should not become an identity provider, a session manager,
or an auth service. The boundary is: substrate answers "is this
signature valid and who does the key belong to?" — not "is this person
allowed to do X?"

### Q2: What is the identity primitive?

**Opaque ID with key fingerprint as the base, optional external binding.**

My preferred shape:

```
{
  "id": "alice",                    // opaque, deployment-chosen
  "key_fp": "sha256:abc123...",     // fingerprint of signing key
  "binding": null | { ... }         // optional: OIDC claim, DID doc, registry entry
}
```

The on-the-wire identifier is `(id, key_fp)`. The `binding` field is
deployment-specific and verifiable independently. An OSS user with no
IdP just uses `(id, key_fp)` and verifies signatures. An enterprise
user adds a `binding` that ties the key to their OIDC subject — the
verifier checks both the signature and the binding.

This keeps the protocol concern (signature verification) separate from
the trust-model concern (what does this identity mean in my org). DIDs
are overkill for v1. OIDC-as-default would be wrong. Public key
fingerprint alone is too rigid (no rotation story). The hybrid with
opaque ID + optional binding handles all four quadrants:
self-sovereign/enterprise × simple/advanced.

### Q3: Deployment model?

**Peer model, strongly agree.** The OSS posture and driving scenario
(solo + auditors) both demand it. The hosted model is a different
project — one that might happen eventually, but designing for it now
would bloat the v1 and slow down the thing that actually matters:
getting the audit trail working for the single-operator case.

For scenario 2 (small team), a shared Postgres substrate database with
schema-per-project isolation is probably sufficient. Each user still
runs their own agent-wake instance; they share a durable store. No
central auth service needed — each user's key identifies them in the
shared DB.

Scenario 3 (cross-org) genuinely needs some rendezvous point, but I'd
argue that's an HTTP webhook relay, not an identity service. User B's
CI fires a webhook; user A's agent-wake receives it; the identity
question is "whose key signed this webhook payload," which is a
sender-gating problem (Q8), not a deployment-model problem.

### Q4: How does identity propagate?

**Record both trigger and actor. Schema suggestion:**

```json
{
  "trigger": { "id": "ci-bot", "key_fp": "sha256:..." },
  "actor": { "id": "alice", "key_fp": "sha256:..." },
  "on_behalf_of": null
}
```

Substrate's existing `on_behalf_of` field maps to this naturally. The
`trigger` is who/what caused the wake; the `actor` is whose harness
executed. When they're the same (solo dogfood), `trigger` and `actor`
are identical and `on_behalf_of` is null. When delegation happens,
`on_behalf_of` carries the delegating party.

On implicit vs. explicit: **both, layered.** The harness should pass
identity metadata to the agent (so the agent can reason about authority
— "this wake came from the CI system, not from a human"). But the
provenance recorder stamps identity independently from the harness's
claims. The provenance stamp is the audit-grade one; the in-context
metadata is advisory.

On prompt injection spoofing the trigger identity: **don't try to solve
this at the context-window level.** The provenance layer verifies
signatures independently. If a prompt injection spoofs the trigger
identity in the agent's context, the provenance log still shows the
real signature chain. The auditor sees the discrepancy. This is the
right failure mode — it's detectable, not silent.

### Q5: Delegation?

**Strict for v1. Per-action consent for v2.**

Strict is the simplest thing that's legally defensible: every tool call
is attributed to whoever is currently operating the harness. Wake
triggers are recorded (they're audit-relevant) but don't transfer
authority. This matches scenario 1 perfectly — the auditor sees "alice
operated the harness, the CI wake arrived at 14:32, alice's agent
executed these tool calls at 14:32-14:35."

Per-action consent (the agent prompts for approval, the approver's
identity is attributed) is the right shape for regulated environments,
but it requires harness cooperation that doesn't exist yet. Claude
Code's `claude/channel/permission` relay is a promising primitive here —
it already forwards structured approval prompts. But building on it is
v2+ work.

Substrate's existing `on_behalf_of` field is a good place to evolve
into a delegation chain. For v1, it stays null or carries a single
delegating identity. For v2, it becomes an array of delegation steps.

### Q6: Auditors?

**Auditors hold public keys; auditor attestations are signed events.**
No separate identity primitive for auditors. An auditor's identity is
just their public key fingerprint, same as any actor. The difference is
their role: they only sign attestation events, never tool-call events.

Auditor attestations being signed events in the log is important — it
means the verification itself is auditable. "Auditor X verified this
log segment on date Y and found it consistent" is a first-class event
with a signature. This composes well: a second auditor can verify both
the original log and the first auditor's attestation.

Read-only access: auditors get read access to the log via the same
mechanism anyone does — they have the public keys of the actors and can
verify signatures. If the log itself is encrypted (not in v1 scope),
then auditor access needs a separate grant mechanism. But for v1, logs
are signed plaintext and anyone with the public keys can verify.

### Q7: Key management?

This is where substrate's existing infrastructure is stronger than the
doc suggests. `_keys.py` already has:

- Key states: active, deprecated, revoked
- Hot-reload from a JSON file
- Environment variable override for secrets
- `verify_key_status()` that rejects revoked keys

What's missing:

- **Generation:** a CLI command (`substrate keygen`) that generates a
  keypair (or for HMAC, a random secret) and writes it to the keys
  file. Trivial to add.
- **Rotation:** already partially handled — mark old key as deprecated,
  add new key as active. The signing envelope already includes
  `key_id`, so old entries remain verifiable with the old key. The
  rotation event itself should be a signed event (sign it with the old
  key, recording the new key's fingerprint).
- **Revocation:** already handled — mark key as revoked. The
  `verify_key_status()` method rejects revoked keys. What's missing is
  a timestamp on revocation (when was it revoked? entries signed before
  revocation should still be valid). This needs a small schema change:
  add `revoked_at: ISO timestamp | null` to the key entry.
- **Recovery:** key loss = identity loss for self-sovereign users. For
  OIDC-backed users, recovery means re-binding a new key to the same
  OIDC subject. This is a v2+ concern and should not block v1.

For v1: CLI keygen, existing rotation via key states, add revoked_at
timestamp, document that key loss is identity loss.

### Q8: Sender gating in multi-user?

**Per-user allowlists with explicit opt-in. Consent is a signed event.**

In single-user, the allowlist is global. In multi-user, each user
maintains their own allowlist. Cross-user triggers require the
receiving user to have explicitly added the sender to their allowlist.
The consent event ("alice added ci-bot to her allowlist") is a signed
event in the provenance log.

This is straightforward with the peer model — each user's agent-wake
instance has its own config. No central allowlist service needed.

### Q9: Backwards compatibility?

**Single-user is the degenerate case. Agree with initial take.**

Identity should always be present. In single-user dogfood, the identity
is "the user who generated the key when they installed substrate." The
`on_behalf_of` field is null. The trigger and actor are the same
person. No migration needed — the schema is the same, the semantics
just degenerate.

The one thing I'd add: make identity optional at the *protocol* level
but default-populated at the *tool* level. If someone runs substrate
without configuring identity (e.g., a quick test), it should still
work — events just won't have attribution. The moment they configure
a key, attribution appears. This matches the existing behavior where
`key_set=None` produces dummy signatures.

### Summary of positions

| Q | Position |
|---|----------|
| 1 | Substrate. Narrow contract: binding + verification, not IdP. |
| 2 | Opaque ID + key fingerprint, optional external binding. Not DID, not OIDC-as-default. |
| 3 | Peer model. Shared DB for small teams. Webhook relay for cross-org. |
| 4 | Record both trigger + actor. In-context metadata is advisory; provenance stamp is authoritative. |
| 5 | Strict for v1. Per-action consent for v2. `on_behalf_of` evolves into chain. |
| 6 | Auditors hold keys. Attestations are signed events. No separate primitive. |
| 7 | CLI keygen, existing key states, add revoked_at timestamp. Key loss = identity loss for v1. |
| 8 | Per-user allowlists. Consent is a signed event. |
| 9 | Single-user degenerates from multi-user. Optional at protocol level, default-populated at tool level. |

### Meta-observation

Substrate is closer to ready than the doc implies. The `on_behalf_of`
field, key lifecycle states, and `key_id`-tagged signatures are already
delegation infrastructure. The main gap is asymmetric signing (BC-196)
— HMAC means the verifier needs the same secret as the signer, which
breaks the auditor model (auditors shouldn't have signing capability).
Asymmetric signing is the dependency that unlocks multi-user for real.
Everything else (opaque IDs, allowlists, propagation schema) can be
layered on top of what exists.

## Related material

- Substrate roadmap items BC-196 (asymmetric signing) and BC-197
  (delegation chain) are the substrate-side counterparts to several
  of the questions above. They are not yet implemented but should be
  designed alongside whatever lands here.
- `/projects/agent-provenance/product-concepts/001-naming.md` —
  agent-provenance is still working-named; the scope statement there
  is the most current articulation of the audit-layer goals.
- `/projects/agent-wake/AGENTS.md` — research findings on the wake
  primitives in Claude Code and opencode that motivated this project.
