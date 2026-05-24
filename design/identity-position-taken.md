# Identity and multi-user — position taken

**Date:** 2026-05-23
**Author:** agent (during design review)
**Status:** For debate; does not modify the open design questions in `identity-and-multi-user.md`.

This document records a concrete position on each question raised in
[identity-and-multi-user.md](identity-and-multi-user.md). It is a
separate file to avoid biasing future readers who should form their
own views before seeing one particular answer.

---

## 1. Identity lives in substrate

Identity belongs in substrate. Substrate already owns event signing
(HMAC-SHA256); identity is the semantic layer of what a signature
*means*. Two projects inventing their own schemes creates a brittle
mapping problem. BC-196 (asymmetric signing) makes this the natural
next step, and BC-197 (delegation chain) builds directly on top.
This does not expand scope as much as it sounds — it makes explicit
what HMAC signing already assumes implicitly.

## 2. Opaque ID with pluggable backing store

The on-the-wire identifier is an opaque string (e.g., a fingerprint,
UUID, or issuer+subject pair). What backs that opaque string is a
deployment concern, not a protocol concern:

| Deployment | Backing store |
|---|---|
| Solo dogfood | Self-generated keypair, local |
| Enterprise / compliance | OIDC provider (Google, Okta, corporate SSO) |
| Self-sovereign | DID, keypair, or whatever the user holds |

This keeps the OSS surface small, avoids forcing enterprise assumptions
on non-enterprise users, and satisfies compliance regimes that demand
"tied to corporate identity" — the deployment picks an IdP and the
protocol doesn't care.

## 3. Peer model first

Matches the stated single-person OSS posture. Scenario 1 ("solo +
auditors") needs no shared service. Scenario 2 ("small team") works
with a shared substrate database (substrate already has schema-per-
project isolation). Scenario 3 ("cross-org delegation") is a v2+
problem. A hosted model is a different project with different
economics — billing, on-call, multi-tenant ops. Don't build it
until it's the only way forward.

## 4. Identity survives the chain cryptographically, not contextually

- **Trigger identity and actor identity are separate fields** in every
  provenance event. If user A triggered the wake and user B's harness
  executed the tool call, both identifiers are recorded side by side.
- **Identity is stamped out-of-band by the provenance recorder**, never
  passed into the agent's context window where prompt injection can
  spoof it. The agent may receive a read-only reference (e.g.,
  `triggered_by: "user:A"`) but never raw credential material.
- **Crossing untrusted boundaries is defense-in-depth**: the wake event
  itself is signed at ingest; the provenance recorder can verify the
  signature independently of what the agent context contains.

## 5. Strict attribution for v1

Every tool call is attributed to whoever is *currently operating the
harness*. Wake triggers are recorded as context but do not delegate
authority. "By B on behalf of A" attribution and per-action consent
flows are v2+ and depend on BC-197 (delegation chain).

This is the simplest model that satisfies the workplace-audit use case:
the operator is always accountable for what their agent does.

## 6. Auditors use the same identity primitive

A key is a key — the only difference between "actor" and "auditor" is
what the bearer is authorized to do. Read-only log access is granted
by adding the auditor's key to an allowlist (the same identity
primitive, different permission). Auditor attestations ("I verified
this log at time T") are signed events appended to the log, making
the verification itself part of the auditable record.

## 7. Key management: local gen, signed rotation/revocation, defer recovery

- **Generation:** local CLI command (`agent-wake key gen`) or imported
  from existing PKI.
- **Rotation:** old log entries remain verifiable. The rotation event
  is a first-class signed event in the log ("key A rotated to key B
  at time T").
- **Revocation:** a signed revocation event is published. Verifiers
  reject signatures from the compromised key after the revocation
  timestamp while still trusting entries before it.
- **Recovery:** deferred past v1. In OIDC-backed deployments the IdP
  handles it. In self-sovereign deployments it's a genuinely hard
  problem — social recovery, sharding, or a recovery delegate are all
  v2+ territory.

## 8. Per-user sender allowlists with signed opt-in

In a multi-user world, gating becomes per-user: user A's sender
allowlist doesn't grant trust to user B by default. Cross-user
triggers require explicit consent — user B opts in to receive events
from user A. The consent itself is a signed, auditable event,
making the trust graph part of the compliance story.

## 9. Single-user is degenerate multi-user

On first run, the system auto-generates an identity (a keypair with
a local backing store). The solo user never sees it. Auditors can
verify it. No separate code paths — the identity field is always
present and the multi-user code is always exercised, even when n=1.
This avoids bitrot and keeps dogfooding identical to the real path.

---

## Summary of the shape

```
substrate owns identity (opaque ID + pluggable backing)
         │
         ▼
agent-wake gated per-user (signed opt-in for cross-user triggers)
         │
         ▼
agent harness (identity stamped out-of-band, never in context window)
         │
         ▼
agent-provenance records (trigger_id, actor_id) side by side
         │
         ▼
auditor verifies → attestation signed into the same log
```

Single-user is n=1 of this picture. Nothing looks different from
the outside.
