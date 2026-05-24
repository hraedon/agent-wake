# Research briefing — agent-provenance and agent-wake

**Audience:** Research-capable agent (web search, document retrieval).
**Scope:** Validate and de-risk the v1 design against prior art and
authoritative compliance sources. **Not** a design exercise.

---

## Background

`agent-provenance` and `agent-wake` are two related projects designing
cryptographic audit trails and external-event-to-session signaling for
AI agents. Three rounds of design review have produced a settled v1
spec. The design is now locked except in narrow places. Your job is
**not to redesign anything.** Your job is to check the design against
prior art and authoritative compliance sources, and report what you
find.

You will produce a research report. You will not propose redesigns. If
you find something that strongly suggests a design change, raise the
question with a citation; the humans decide.

---

## Mandatory pre-reading

Read these before beginning research. The design context matters —
without it, you will produce generic survey work that isn't useful.

1. `/projects/agent-wake/design/v1-implementation-spec.md` — the
   settled v1 design. Pay special attention to §1 (locked positions),
   §3 (event schemas), §4 (verifier contract).
2. `/projects/agent-wake/design/v0-implementation-plan.md` — what's
   being built now for wake-on-event (no signing yet).
3. `/projects/agent-provenance/README.md` — driving use case
   (workplace audit) and the trust-model layering.
4. `/projects/substrate/breadcrumbs/resolved/196-*.md`,
   `197-*.md`, `198-*.md` — the substrate-side dependencies.
5. `/projects/substrate/breadcrumbs/214-*.md` through
   `221-*.md` — new BCs filed against substrate.

You may skim the round-by-round review documents in
`/projects/agent-wake/design/` for context, but they are not load-
bearing for your research.

---

## Research threads

Four threads, ordered by leverage. Spend most of your time on threads
1 and 2; threads 3 and 4 are smaller checks.

### Thread 1 — Prior art for cryptographic agent / automation provenance

**Question:** Is anyone else building, or has anyone published,
something resembling agent-provenance? What schemas and tooling do
they use? Where does the v1 spec's design align with, depart from, or
duplicate existing work?

**Specifically check:**

- **AWS CloudTrail log-file integrity validation.** The
  agent-provenance README cites this as the structural analog. What
  is the actual digest file format, the validation flow, the auditor
  workpaper that consumes it? How do their digest chains handle
  retention boundaries and gap detection?
- **Sigstore** (cosign, fulcio, rekor) — particularly the
  **Sigstore bundle format** (the protobuf-based bundle that became
  stable around 2024). This is a battle-tested format for "signed
  artifact + verification material + transparency-log proof."
  Could agent-provenance's bundle format adopt or extend it instead
  of inventing a new one?
- **SLSA (Supply-chain Levels for Software Artifacts).** Provenance
  attestation schema, predicate types, the in-toto attestation
  format. SLSA is for build provenance, but the data model is close
  to what agent-provenance needs.
- **in-toto attestations.** Predicate types, statement format,
  attestation bundling.
- **Trillian / Certificate Transparency.** Merkle-tree-based
  append-only logs with verifiable consistency proofs. The
  `checkpoint` event type in BC-221 is a CT-style construct;
  understanding the CT proof model would help.
- **OpenTelemetry traces with cryptographic signing.** Is there a
  published OTel extension that signs span exports? The
  agent-provenance README mentions OpenTimestamps in passing
  (trust-model layer 6) — what's the actual OpenTimestamps
  protocol and is it usable here?
- **Academic work on signed agent execution traces, verifiable
  AI agent action logs, or LLM provenance.** This is a younger
  field; papers from 2024–2026 may be highly relevant.

**Output for this thread:** For each system listed, a 3–5 sentence
summary covering (a) what it is, (b) whether its schema or tooling is
relevant to agent-provenance, and (c) if relevant, a specific
implication for the v1 spec (file and section reference). Skip
systems that turn out to be irrelevant — say so briefly.

**Highest-value finding would be:** "Sigstore bundle format covers
exactly the use case in agent-provenance's verifier contract §4.
Recommend adopting it instead of the hand-rolled format in v1 spec
§4." Or its opposite: "Sigstore bundles assume a registry-based trust
root incompatible with agent-provenance's peer model. Keep the
hand-rolled format."

### Thread 2 — Compliance regime specifics (highest priority)

**Question:** The v1 design rests on satisfying real workplace-audit
requirements (SOX, HIPAA, SOC 2 Type II, internal IT audit). The
design reviews cited these regimes from training-data summaries, not
authoritative sources. What do these audit regimes *actually* require
from automated-system audit logs?

**Specifically check:**

- **SOX §404 (Sarbanes-Oxley, IT General Controls).** What artifact
  does an external auditor (Big Four or equivalent) actually consume
  during an IT GCC audit? Is it a live database read, a log export,
  a hash-chained file, something else? Cite a recent (post-2022)
  auditor guide or AICPA document if possible.
- **AICPA SOC 2 Trust Services Criteria (the current version).**
  What do criteria CC7.2 (system monitoring) and CC4.1 (control
  monitoring) require in terms of log retention, integrity, and
  reviewer access? The criteria are publicly available from AICPA.
- **HIPAA §164.312(b) (audit controls).** What do recent OCR
  enforcement actions reveal about audit-control implementations
  that *failed* an investigation? Failed implementations are
  diagnostic — they tell you what the standard actually requires
  in practice.
- **FFIEC IT Examination Handbook (Information Security
  Booklet).** Section on audit logs. Banking regulators are
  prescriptive about log integrity; their guidance may be the
  most concrete.
- **PCI DSS v4.0 Requirement 10 (logging).** Card-industry
  logging requirements, especially around access control to logs
  and tamper-evidence.
- **ISO/IEC 27001 Annex A 12.4 (logging and monitoring).**
  International equivalent; check if there's tighter language than
  the US-focused regimes.
- **EU AI Act audit requirements.** Recent (2024+) — does it
  specify anything about agent action logging that affects
  agent-provenance positioning?

**Output for this thread:** For each regime, what the audit artifact
literally is (live system access? signed export? what format?), what
the auditor's workpaper expectation looks like, and any specific
implication for the v1 verifier contract (§4) or bundle format (§3).

**Highest-value finding would be:** Concrete evidence about
whether direct-DB-read or signed-bundles is the dominant pattern in
real audits — this affects v1's hybrid model.

The design currently leans heavily on the "bundles for evidence"
side; if research shows that's right, lock it in. If research shows
auditors actually want live access more often than reviewers
assumed, that's a meaningful update.

### Thread 3 — RFC 3161 TSA landscape

**Question:** The v1 spec defers TSA choice to deployment config and
mentions FreeTSA and DigiCert as examples. Are those actually good
choices? What's the operational landscape?

**Specifically check:**

- **Free public TSAs.** FreeTSA (freetsa.org), Apple's TSA used by
  notarization, DigiCert's free TSA, any others. Rate limits,
  uptime SLA (or lack thereof), certificate chain trust paths,
  whether they're acceptable to SOX/SOC 2 auditors.
- **Commercial TSAs.** DigiCert, GlobalSign, Sectigo, IdenTrust.
  Cost models (per-token, per-batch, subscription), audit-grade
  certifications (eIDAS QTSP, WebTrust for TSAs).
- **Self-hosted TSAs.** Are there mature open-source TSA
  implementations? What's the regulatory acceptance of
  self-hosted TSAs in a SOX audit?
- **RFC 3161 vs newer alternatives.** Is RFC 3161 still the
  state-of-the-art for trusted timestamping, or has something
  superseded it for this use case (e.g., RFC 5816, blockchain-
  based notarization)?

**Output for this thread:** A short table: which TSAs are
production-grade, which are appropriate for SOX-level audit
evidence, which have rate limits that would bite at the 15-minute
batch interval the v1 spec recommends. Plus one recommendation:
which TSA(s) should v1 documentation cite as the suggested default?

### Thread 4 — DID rotation mechanics, decoupled from resolution

**Question:** Round-3 consensus rejected DIDs because they add a
discovery/resolution layer substrate doesn't need. But the DID rotation
mechanism specifically may still be worth borrowing. **Does DID Core
specify a key-rotation/key-history mechanism that agent-provenance's
rotation-chain design (`key_rotation` events in the log) could adopt
the semantics of, without importing DID resolution?**

**Specifically check:**

- DID Core 1.0 specification, section on key rotation and
  verification method updates.
- `did:key`, `did:web`, `did:plc` (the Bluesky/AT Proto DID
  method) — their actual rotation mechanisms in practice.
- W3C Verifiable Credentials data model — how it handles
  signed-artifact key rotation independently of DID resolution.

**Output for this thread:** Is the rotation chain in v1 spec §3
materially equivalent to what DIDs do? If yes, is there a benefit to
adopting DID terminology even without DID resolution (interop with
DID-aware tooling)? If no, the current design stands without owing
anyone an explanation.

This is a smaller thread. Don't spend more than a fraction of your
time on it.

---

## Format constraints

- **Markdown only.** No LaTeX. ASCII or Mermaid diagrams welcome.
- **Citations matter.** For every claim about an external system,
  cite the source (URL or document name + section). Claims without
  citations will be discounted.
- **Honesty over completeness.** If a thread turns up nothing
  relevant, say so explicitly. "Checked X, Y, Z; nothing changes the
  design" is a valid output. Padding to look comprehensive is worse
  than admitting empty hands.
- **No design proposals.** You are researching prior art, not
  redesigning. If your research suggests a change, raise the
  question with the citation; do not propose the new design.
- **Implications must be specific.** "This is relevant" is not
  useful. "This pattern from Sigstore bundle format applies to v1
  spec §3 bundle storage; if adopted, the implication is X" is
  useful. Tie every implication to a file and section.

## Required output structure

```
research-report.md

1. Executive summary (5–10 bullets, what changed your understanding)
2. Thread 1: Prior art findings
   - For each system checked: 3–5 sentence summary, relevance,
     implication
3. Thread 2: Compliance regime findings
   - For each regime: what the audit artifact actually is, where
     the v1 spec aligns or diverges
4. Thread 3: TSA landscape findings
   - Short comparative table + recommendation
5. Thread 4: DID rotation mechanics
   - Brief answer to the equivalence question
6. Questions for humans
   - List of questions your research raised that you cannot
     resolve. Be specific.
7. What you did NOT check
   - Briefly note what you considered checking and skipped, with
     reasons. This is anti-padding insurance.
```

Save as `/projects/agent-wake/design/research-findings.md`.

---

## Out of scope

Do not engage with any of these. They are settled and re-litigating
them wastes your time.

- The eleven locked positions in v1 spec §1.
- The choice of opaque ID + attestation-type over DIDs (other than
  thread 4's specific question about rotation mechanics).
- The choice of peer model over hosted.
- The choice of HMAC for v1 with Ed25519 as v2.
- Whether to build agent-wake at all, or whether to merge it with
  agent-provenance.
- Whether sf2 should adopt agent-wake (a separate scoped question,
  not for research).
- General "AI agent landscape" surveys, market research, or
  competitive analysis. Out of scope.
- Channels API research beyond what's needed for thread 1
  (CloudTrail-style log integrity).
- opencode plugin ecosystem research. The plugin docs are sufficient.

If you find yourself drifting toward any of these, stop and refocus.

---

## A note on your role

Three rounds of human-curated cross-lineage review already produced
the v1 design. Your job is not to add a fourth round. You are the
external-validation layer: checking whether the in-room consensus
aligns with what exists in the broader world. This is genuinely
useful when done well — independent research routinely finds gaps
that internal review misses — but it requires discipline. If you
find yourself wanting to argue with a locked decision, that's a
signal to redirect to "is there published prior art that supports my
concern?" If there isn't, drop it.

Good research changes minds with citations. Generic survey work
doesn't. Aim for the former.
