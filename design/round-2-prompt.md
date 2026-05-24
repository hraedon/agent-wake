# Round 2 review prompt

Copy from the horizontal rule below to share with the next round of
reviewers. Each reviewer should be a different model lineage from the
ones already represented (Claude, opencode-via-mimo, Antigravity, the
two un-attributed agent reviewers).

---

You are one of several reviewers from different model lineages being
consulted on a design decision. The first round produced strong
convergence on most questions, which we suspect is partly an artifact
of reviewers reasoning over the same prompt rather than the underlying
code. We want you to do better than that.

## Mandatory pre-reading

Before forming any positions, you must read:

1. `/projects/agent-wake/design/identity-and-multi-user.md` — the
   original design questions document. **Note the appended sections
   from the previous round.** You are not asked to re-debate
   questions where prior reviewers reached consensus.

2. `/projects/substrate/src/substrate/_signing.py` — the existing
   event-signing implementation. Pay attention to the `on_behalf_of`
   field and the signature envelope shape.

3. `/projects/substrate/src/substrate/_keys.py` — the existing key
   management code. Pay attention to key lifecycle states
   (`active`, `deprecated`, `revoked`) and how key resolution works.

4. `/projects/substrate/breadcrumbs/` — search for `BC-196`
   (asymmetric signing) and `BC-197` (delegation chain). These are
   the tracked design problems that this work depends on.

5. `/projects/agent-provenance/README.md` — the consumer project's
   scope statement. Note especially the workplace-audit driving
   use case.

**Your output will be discounted if it does not cite specific files,
functions, or breadcrumbs by name.** Position-taking without code
grounding produced redundant output last round; we are not interested
in another round of the same.

## What is already decided

Nine of the design questions reached cross-reviewer consensus and are
not open for re-litigation. They are recorded as the "Proposed
positions," "Agent positions," and "Reader input: opencode agent"
sections of `identity-and-multi-user.md`. Read them; do not rewrite
them. If you have a sharp objection to one of the supposedly-settled
positions, state it briefly, but the default is to accept them.

## What we want you to answer

Three real disagreements remain. For each, prior reviewers split or
hand-waved. Your job is to argue for one specific path with
code-grounded reasoning.

### Question A: Identifier format on the wire

The candidates:

- **Raw key fingerprint** (e.g., `sha256:abc123...` of an ed25519
  public key). Minimal, no spec dependency, hard to extend.
- **Opaque ID + attestation-type enum** (e.g., `{id, type: "key" |
  "oidc" | "did"}`). Pluggable; protocol stays simple; deployment
  picks the backing.
- **DID URI** (e.g., `did:key:z6Mku...`, `did:oidc:...`). Standards
  compliance; richer rotation/discovery story; more spec surface.

Cite which substrate code (signing envelope shape, key resolution
path) would have to change for each option, and how much. The one
that requires the least invasive substrate change while preserving
the auditor model is probably right.

### Question B: Auditor access model

The candidates:

- **Direct verification.** Auditor has read access to the live log
  and verifies signatures against actor public keys. Assumes the log
  is signed plaintext.
- **Durable audit bundles.** Periodic signed JSON exports of log
  segments. Auditor verifies bundles offline; never touches the live
  system.
- **Hybrid.** Direct verification for solo + auditor scenario;
  bundles for compliance / arms-length scenarios.

The workplace-audit use case is the driving scenario — find the
audit-regime references in `agent-provenance/README.md` and reason
about which model actually satisfies a real compliance audit
(SOX, HIPAA, internal IT audit, etc.). Cite specifics, not
hand-waves.

### Question C: Recovery mechanism (for v2, but design now)

V1 ships without recovery — key loss equals identity loss. V2 needs
something. The candidates:

- **Second-device recovery key.** Generated at setup, stored
  offline (USB, printed QR, second machine).
- **Social recovery.** N-of-M trusted peers can co-sign a key
  replacement.
- **OIDC-only.** Recovery is delegated entirely to the IdP for
  enterprise; self-sovereign users accept identity loss.
- **No recovery, ever.** Identity loss is the cost of self-sovereignty;
  document and accept.

This is a v2 question, so the right answer is the one that doesn't
preclude v1 decisions. Cite which v1 schema or substrate primitive
would have to be retrofitted for each option.

## Required output format

Your response must include:

1. **Pre-reading attestation.** List the files you actually read,
   one line each, with the most surprising thing you found in each.
   "Nothing surprising" is a valid answer if true.

2. **Substrate code findings.** What does substrate already
   support that the design doc treats as future work? What does it
   *not* support that the design doc assumes? Cite by file and
   function name.

3. **Position on Question A.** With substrate-code grounding.

4. **Position on Question B.** With reference to the actual audit
   regime that the workplace-audit use case implies.

5. **Position on Question C.** With reference to which v1 decisions
   it would constrain.

6. **Disagreements with consensus (optional).** If you think the
   "settled" nine include a wrong answer, name it and argue
   briefly. Do not relitigate items where you agree.

7. **Things the design doc misses entirely.** Open-ended. What
   should be a question that isn't?

## Format constraints

- Markdown. No LaTeX equations (they don't render in our viewers).
- Mermaid diagrams allowed; ASCII diagrams preferred.
- File-and-function citations in code fences or backticks.
- Length budget: aim for thorough but not exhaustive. If you find
  yourself restating the design doc, stop and cut.
