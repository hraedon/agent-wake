# Research briefing round 2 — gaps from round 1

**Audience:** Same research-capable agent that produced
`research-findings.md` (2026-05-23).
**Scope:** Two targeted follow-ups. Same rules as round 1 — validate
against prior art and authoritative sources, do not redesign, cite
everything.

---

## Context

Round 1 (`research-findings.md`) validated the v1 bundle format and
event schemas against CloudTrail, Sigstore, in-toto/SLSA, Trillian/CT,
the major compliance regimes, the TSA landscape, and DID rotation
mechanics. Most findings supported the locked design. Two gaps were
identified after review:

1. The compliance thread validated **bundle-as-evidence** but did not
   probe whether auditors will accept bundles whose signing keys have
   **no externally-attested trust root** (no commercial CA, no
   WebTrust-audited TSA on the signing key itself). The peer model is
   the actual differentiator; if it fails auditor acceptance, the
   design fails its driving use case.
2. Round 1's title scoped the work to "agent-provenance / agent-wake"
   but Threads 1–4 only covered agent-provenance. The wake-signaling
   design got no prior-art coverage.

These are the only two threads for round 2. Keep it tight.

---

## Mandatory pre-reading (delta from round 1)

You already read the v1 spec, v0 plan, and agent-provenance README in
round 1. For round 2, also read:

1. `/projects/agent-wake/README.md` — the wake project's own scope
   and positioning, if present.
2. `/projects/agent-wake/design/v0-implementation-plan.md` §
   covering the wake-signaling protocol (the external-event-to-session
   channel, not the provenance signing).
3. The reference memory
   `/home/itadmin/.claude/projects/-projects/memory/reference-wake-and-hooks-across-harnesses.md`
   for the harness-side primitives (Claude Code channels, opencode
   `session.prompt`, tool-interception hooks). This is internal context,
   not a source to cite — it tells you what wake actually composes with.

Skip everything else from the round 1 reading list unless you find
yourself needing to re-check a specific claim.

---

## Research threads

### Thread A — Auditor acceptance of peer-signed evidence (highest priority)

**Question:** When the artifact handed to an external auditor is
signed by a key that has **no commercial CA chain, no WebTrust-
audited TSA on the signing key's certificate, and no hosted
transparency log**, do real-world auditors accept it as evidence?
Under what conditions?

The v1 design produces bundles signed by per-deployment keys (HMAC in
v1, Ed25519 in v2). The trust root is whatever the deploying
organization vouches for — typically the same IT department whose
controls are being audited. A commercial TSA may anchor the
*timestamp*, but the *signing key itself* is self-managed. Round 1's
§3 essentially assumed this is fine because "bundles are accepted as
exports." That conflates two different trust claims.

**Specifically check:**

- **What evidence do SOX ITGC auditors actually require about the
  *signing key* used on exported logs?** When a client exports a
  hash-chained log signed by an internal key, does the auditor
  require: (a) nothing about the key, (b) the key was generated in
  an HSM, (c) the key's certificate chains to a recognized CA, (d)
  the key was attested by a third party, (e) something else?
  Cite a PCAOB inspection report, Big Four practice guide, or
  AICPA audit guide.
- **SOC 2 Type II reports on log integrity controls.** What language
  do real SOC 2 reports use to describe self-managed signing keys?
  Search published SOC 2 reports (vendors often publish redacted
  versions) for the control description around "log signing" or
  "audit log integrity." Does the report describe key custody and
  if so, how?
- **Internal IT audit (non-external-attestation) acceptance.**
  Internal IT audit is a less demanding bar than SOX/SOC 2 external
  audit. Is there published guidance from IIA (Institute of Internal
  Auditors) on what self-signed log integrity controls satisfy
  internal audit? This may be the realistic acceptance bar for v1.
- **CloudTrail as precedent (revisit).** AWS CloudTrail is signed by
  an AWS-managed key, not a customer key. The validation flow trusts
  AWS's signing key by virtue of trusting AWS. Is there published
  guidance about whether customers can substitute a customer-managed
  signing key (e.g., for self-hosted on-prem CloudTrail equivalents)
  and have that accepted?
- **Closest analog: file integrity monitoring (FIM) tools.** Tools
  like Tripwire, OSSEC, Wazuh produce signed log-integrity reports
  using internally-managed keys. They are widely accepted in PCI DSS
  and SOX audits. What is the auditor-facing trust model? This is
  probably the strongest precedent for peer-mode trust roots, if it
  holds up.

**Output:** A direct answer to the central question — yes, no, or
"conditionally yes if X." If conditionally, what the X is. Include
the FIM-tool analog explicitly, because if it works for Tripwire it
likely works for agent-provenance.

**Highest-value finding would be:** "FIM tools (Tripwire, Wazuh) are
accepted in SOX/PCI audits with internally-managed signing keys,
provided [conditions]. Agent-provenance can position as a FIM-class
control and inherit that acceptance." Or its opposite: "Self-signed
log integrity is accepted only for internal audit, not external
attestation. External SOX/SOC 2 audits require a third-party-attested
signing key. The peer model is not externally-audit-grade without
additional trust infrastructure."

The opposite finding would be a serious result for the project. Do
not soften it if that's what you find.

### Thread B — Prior art for external-event-to-session signaling

**Question:** agent-wake delivers external events into a running AI
agent session across harness boundaries (Claude Code, opencode, etc).
Has anyone published or built a comparable primitive? What protocols
or patterns does prior art use?

The wake design is not provenance — it's a wake-on-event channel
that lets external systems (cron, webhooks, file changes, MCP
servers, other agents) signal into a long-lived agent session
without depending on a specific harness's IPC.

**Specifically check:**

- **Webhook delivery patterns** as a baseline. Idempotency keys,
  retry semantics, delivery acknowledgement (Stripe, GitHub webhooks
  as reference designs). What guarantees do these provide and what's
  the failure model?
- **Event-driven workflow engines.** Temporal, Restate, Inngest,
  Trigger.dev. These deliver external events into running workflows.
  How do they identify the workflow instance? How do they handle
  re-entrancy? How do they handle the workflow being asleep vs
  awake?
- **Server-Sent Events / WebSocket session resumption.** When a
  long-lived session needs to receive out-of-band events, the
  closest mainstream pattern is SSE or WebSocket. Has anyone built
  a "session inbox" abstraction that survives reconnect? Look at
  Phoenix Channels, ActionCable, Centrifugo, or Pusher.
- **MCP (Model Context Protocol) external triggers.** Does MCP
  itself have a primitive for an external party to push events
  into a session? Or is MCP strictly request-response from the
  client?
- **Agent-to-agent messaging protocols.** Any published work on
  cross-agent / cross-harness messaging? E.g., does AutoGPT,
  CrewAI, LangGraph, or the Google AgentSpace SDK have a
  primitive for external-event delivery into a running agent?
- **Process-supervision-style wakeups.** systemd socket activation,
  inotify, dbus signal delivery. These are mechanically related —
  wake a process when something happens. Is there a clean abstraction
  layer above these that agent-wake might align with?

**Output:** For each pattern, a 3–5 sentence summary covering (a)
what it does, (b) whether its protocol or semantics overlap with
agent-wake's design, (c) if overlap exists, a specific implication
for the v0 wake plan (file and section reference). Skip patterns
that turn out to be irrelevant.

**Highest-value finding would be:** Either a specific published
protocol that agent-wake should consider aligning with (with the
implication clearly stated), or confirmation that no published
primitive covers this exact problem — which would mirror the
"academic gap" finding for agent-provenance and similarly suggest
publishing prior art.

---

## Format constraints

Same as round 1. Markdown only, citations required, honesty over
completeness, no design proposals. If a thread turns up nothing
relevant, say so explicitly.

## Required output structure

```
research-findings-round2.md

1. Executive summary (3–6 bullets, what's new since round 1)
2. Thread A: Peer-signed evidence acceptance
   - Direct answer to the central question
   - FIM tool analog assessment
   - Per-regime detail where it differs from round 1's findings
3. Thread B: Wake-signaling prior art
   - For each pattern: summary, relevance, implication
4. Questions for humans
5. What you did NOT check
```

Save as `/projects/agent-wake/design/research-findings-round2.md`.

---

## Out of scope

- Everything ruled out in round 1's out-of-scope section.
- Re-litigating round 1's findings. If round 1 said X and you now
  disagree, raise it in "Questions for humans," do not rewrite the
  finding.
- The substrate breadcrumb directory was empty in round 1; do not
  spend time looking for it again.
- Pricing / commercial procurement details for TSAs or HSMs.
- Wake protocol *design* — only prior art and analog patterns.

---

## A note on Thread A

Thread A may produce an uncomfortable finding. The peer model is a
locked v1 position; round 1 was instructed not to challenge it. This
round is different: you are not being asked to challenge the peer
model, you are being asked to **report honestly on whether external
auditors will accept its output as evidence.** If the answer is "not
for external SOX/SOC 2 without additional infrastructure," say so
plainly. The humans will decide what that means for positioning. Do
not soften the finding to preserve the design.

This is the most valuable thing round 2 can produce. Treat it that
way.
