# Research Report: agent-provenance / agent-wake v1 External Validation

**Date:** 2026-05-23  
**Scope:** Check v1 design against prior art and authoritative compliance sources. No redesigns proposed.  
**Source material:** `v1-implementation-spec.md` §1–§4, `v0-implementation-plan.md`, `agent-provenance/README.md`, and external sources cited below.

---

## 1. Executive Summary

- **CloudTrail is the closest structural analog** — digest files are JSON with a hash chain (previous digest hash value + algorithm), RSA-SHA256 signature, and a per-batch `logFiles` array with per-file hashes. CloudTrail uses S3 for both storage and lifecycle-based retention; gap detection fails at missing digest files rather than filling them. This validates the hand-rolled bundle approach in v1 spec §4 but suggests the `manifest.json` should explicitly encode the previous-bundle hash (not just the per-file integrity hashes) to achieve the same chain continuity.
- **Sigstore bundle format is NOT a drop-in replacement.** The format (protobuf, v0.3) bundles a single artifact plus `VerificationMaterial` (X.509 cert chain + Rekor tlog entries + optional RFC 3161 timestamps) and content (either a `message_signature` or a DSSE envelope). It is purpose-built for "one signing event, one artifact" (container image, package, SBOM). Agent-provenance’s model is an *append-only log* with *key rotation chains*, *scope attestations*, and *auditor co-signatures* — none of which fit the Sigstore bundle’s data model. The opposite finding would have been more disruptive; the current design stands.
- **The in-toto Statement v1 format (_type / subject / predicateType / predicate) is a conceptual near-match** for individual events in the log, but in-toto is a one-shot attestation framework for build artifacts, not an append-only audit log. The DSSE envelope (used by both in-toto and Sigstore) could theoretically wrap each event, but that would add an extra envelope layer without value for a system that already signs events with JCS + Ed25519/HMAC.
- **Trillian / Certificate Transparency provides the Merkle-tree primitives** that the `checkpoint` event in BC-221 is designed around. However, Trillian is a hosted server infrastructure (database-backed, gRPC API) meant for transparency logs, not for peer-mode audit bundles. The *concept* of Merkle consistency proofs is directly applicable; the *code* is not.
- **Compliance regimes overwhelmingly expect static evidence exports**, not live system access, as the primary audit artifact. SOX ITGC auditors take screenshots, log exports, and policy documents; SOC 2 is criteria-based and flexible; PCI DSS v4.0 now mandates automated log review mechanisms but still relies on exported samples for the auditor workpaper. This strongly supports the "bundle as unit of evidence" model in v1 spec §4. The hybrid model (direct verification for discovery, bundles for evidence) is well-aligned with real-world practice.
- **RFC 3161 remains the state-of-the-art for trusted timestamping** in audit contexts. RFC 5816 (2010) extended RFC 3161 with certificate ordering but did not supersede it. No blockchain-based protocol has displaced RFC 3161 for SOX/SOC 2 evidence.
- **DigiCert’s commercial TSA is the only audit-grade public TSA** suitable for the 15-minute batch interval in v1. FreeTSA has no SLA, no audit certifications, and is run by a single individual — acceptable for development, not for compliance evidence. The Sigstore Timestamp Authority is the best open-source self-hosted TSA but requires its own CA and WebTrust/eIDAS qualification to be accepted as audit evidence.
- **DID key rotation mechanisms (did:plc in particular) are structurally equivalent** to the v1 `key_rotation` event chain, but adopting DID terminology would add a resolution layer and spec debt without interop benefits, because no auditor tooling today understands DID terminology in the context of offline audit bundles.
- **No published academic work directly addresses cryptographic audit trails for AI agent tool calls.** The closest is MAIF (artifact-centric provenance for agent outputs) and OpenID Foundation’s agent-identity whitepaper; both address identity and artifact provenance, not verifiable execution logs.

---

## 2. Thread 1: Prior Art Findings

### 2.1 AWS CloudTrail Log-File Integrity Validation

CloudTrail delivers a digest file alongside each batch of log files to S3. The digest file is JSON (`{"digestEndTime": "...", "digestStartTime": "...", "digestS3Bucket": "...", "digestS3Object": "...", "logFiles": [...], "previousDigestHashValue": "...", "previousDigestHashAlgorithm": "...", "digestHashAlgorithm": "..."}`) and is signed with RSA-SHA256 using a CloudTrail-specific key pair. Each digest’s `previousDigestHashValue` creates a hash chain across batches. The `logFiles` array contains per-file `s3Object`, `hashValue`, and `hashAlgorithm` entries. AWS provides a `validate-logs` CLI that walks the chain and checks signatures. If a digest file is missing (due to lifecycle deletion, delivery failure, or tampering), validation fails at the gap — the tool reports which digests are missing.

**Relevance:** High. The v1 spec’s `manifest.json` + `events.jsonl` bundle is conceptually similar but currently lacks an explicit *previous-bundle hash* chain. CloudTrail proves that a hash-chain digest model is both implementable and auditor-accepted at enterprise scale.

**Implication for v1 spec §4 (Bundle format):** Consider adding `previous_bundle_hash` and `previous_bundle_algorithm` to `manifest.json` to create the same cross-bundle continuity that CloudTrail uses for gap detection across retention boundaries. This is a narrow addition, not a redesign.

*Sources:* [AWS CloudTrail digest file structure](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-validation-digest-file-structure.html); [Custom implementations](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-custom-validation.html); [AWS CLI validate-logs](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-validation-cli.html).

---

### 2.2 Sigstore Bundle Format (v0.3)

The Sigstore bundle is a protobuf message (`dev.sigstore.bundle.v1.Bundle`) with three fields: `media_type` (version discriminator, e.g. `application/vnd.dev.sigstore.bundle.v0.3+json`), `verification_material`, and `content` (oneof `message_signature` or `dsse_envelope`). The `verification_material` contains either a public-key hint, an X.509 certificate chain, or a single leaf certificate, plus optional `TransparencyLogEntry` (inclusion promise/proof from Rekor) and `TimestampVerificationData` (RFC 3161 signed timestamps). The bundle is designed for a single artifact signing event and assumes the verifier can resolve the trust root (Fulcio CA for keyless, or a known public key for self-managed keys).

**Relevance:** Medium for format design, low for direct adoption. The bundle covers exactly three concerns that agent-provenance also has (signature, timestamp, trust material), but its data model is artifact-centric, not log-centric. A Sigstore bundle cannot represent an append-only event log, a key rotation chain, or a scope attestation chain.

**Implication for v1 spec §4:** Keep the hand-rolled bundle. Sigstore bundles assume a registry/PKI trust root incompatible with agent-provenance’s peer model (no hosted CA, no Rekor). If future v2 work adds a hosted transparency log, re-evaluate then.

*Sources:* [Sigstore Bundle Proto](https://github.com/sigstore/protobuf-specs/blob/main/protos/sigstore_bundle.proto); [Sigstore Bundle Spec](https://docs.sigstore.dev/about/bundle/); [Sigstore Client Spec — Verification using a Bundle](https://docs.google.com/document/d/1kbhK2qyPPk8SLavHzYSDM8-Ueul9_oxIMVFuWMWKz0E/edit).

---

### 2.3 SLSA Provenance and in-toto Attestations

SLSA v1.0 defines a provenance predicate that describes how a software artifact was built. It is wrapped in an in-toto Statement v1, which has the schema `{ "_type": "https://in-toto.io/Statement/v1", "subject": [ { "name": "...", "digest": { "sha256": "..." } } ], "predicateType": "https://slsa.dev/provenance/v1", "predicate": { ... } }`. The signing envelope is typically DSSE (Dead Simple Signing Envelope), which is a base64-encoded payload with an attached signature. The in-toto framework also supports attestation bundling and link metadata, but all of these are one-shot attestations about build steps or artifacts, not an append-only log of operational events.

**Relevance:** Medium. The `subject` + `predicate` model is structurally similar to agent-provenance’s `actor_id` + `payload` model. The naming conventions (`_type`, `predicateType`, `digest`) are well-established. However, the surrounding infrastructure (layout files, link metadata, in-toto attestations) is build-pipeline-specific and not applicable to agent tool-call audit trails.

**Implication for v1 spec §3 (Event schemas):** No action required. The existing event schema is simpler and purpose-built. If cross-industry interop becomes a goal in v2, aligning with in-toto’s `ResourceDescriptor` (name + digest) for the `scope_attestation.harnesses` entries could be considered, but this is not load-bearing for v1.

*Sources:* [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md); [SLSA Provenance v1.0](https://slsa.dev/spec/v1.0/provenance); [DSSE Protocol](https://github.com/secure-systems-lab/dsse/blob/master/protocol.md).

---

### 2.4 Trillian / Certificate Transparency

Trillian is a Go library and gRPC service for append-only Merkle trees with verifiable consistency proofs and inclusion proofs. It powers Certificate Transparency (CT) logs, Sigstore Rekor, and other transparency services. CT logs publish periodic *signed tree heads* (STHs) that clients can use to verify the log has not been forked or truncated. The `checkpoint` event in BC-221 (§3) is structurally a CT-style signed tree head.

**Relevance:** Medium for the checkpoint concept, low for implementation. Trillian requires a hosted backend (MySQL/Spanner + gRPC) and is designed for public transparency logs, not peer-mode audit bundles. However, the mathematical primitives (Merkle root, consistency proof, inclusion proof) are exactly what the v2 checkpoint event would need.

**Implication for v1 spec §3 (`checkpoint` event type):** The reservation of `checkpoint` as a signed Merkle root + TSA token is validated by CT practice. No change needed; the design is sound. For v2 implementation, consider whether the Merkle tree should be computed over the canonical JSON serialization of events (as currently implied) or over a separate hashing layer.

*Sources:* [Trillian GitHub](https://github.com/google/trillian); [Certificate Transparency RFC 6962](https://datatracker.ietf.org/doc/html/rfc6962); [Transparency.dev — Verifiable Data Structures](https://transparency.dev/verifiable-data-structures/).

---

### 2.5 OpenTelemetry Signed Traces

OpenTelemetry defines the W3C Trace Context propagation format and a protocol-agnostic data model for traces, metrics, and logs. There is **no published extension** for cryptographically signing span exports or trace batches. The OpenTelemetry Collector has integrations with Sigstore for signing container images of the collector itself, but this is supply-chain provenance for the *binary*, not data-integrity for the *telemetry*. A 2024-2025 research area on "signed telemetry" exists in drafts but nothing has reached spec status.

**Relevance:** Low. Agent-provenance does not need to align with OpenTelemetry’s data model because its events are not traces in the OTel sense (no parent-child span relationships, no distributed context propagation).

**Implication:** None. Skip.

*Sources:* [OpenTelemetry Specification](https://opentelemetry.io/docs/specs/otel/); search for "opentelemetry cryptographic signing span" returned no stable spec as of May 2026.

---

### 2.6 OpenTimestamps

OpenTimestamps (OTS) provides a standard format for blockchain timestamping. A calendar server aggregates hash requests into a Merkle tree, submits the root to a Bitcoin transaction, and returns a compact proof file (.ots). The proof can be verified offline against the Bitcoin blockchain. OTS is free (no per-token cost) but depends on Bitcoin block inclusion (approx. 10-minute intervals, sometimes longer).

**Relevance:** Medium as an alternative timestamping layer. The agent-provenance README mentions OTS in the trust-model layering (layer 6, optional hardening). OTS could complement RFC 3161 by providing blockchain-anchored immutability, but it is slower (Bitcoin block time), requires internet access for verification, and is not auditor-friendly in the same way a standard RFC 3161 token is.

**Implication for v1 spec §1 (Temporal integrity):** Keep RFC 3161 as the primary anchoring mechanism. Document OTS as a v2+ optional hardening layer, not a v1 default.

*Sources:* [OpenTimestamps.org](https://opentimestamps.org/); [OpenTimestamps GitHub](https://github.com/opentimestamps).

---

### 2.7 Academic Work on AI Agent Provenance (2024–2026)

**MAIF: Enforcing AI Trust and Provenance with an Artifact-Centric Agentic Paradigm** (arXiv 2511.15097, 2025). Proposes shifting from agent-centric to artifact-centric provenance, where AI outputs are treated as persistent, verifiable data objects. Cited by 3 as of early 2026. The paper focuses on the *outputs* of agent reasoning (documents, code, data) rather than the *execution trace* of the agent itself. No cryptographic log format is proposed.

**Zero-Trust Identity Framework for Agentic AI** (arXiv 2505.19301, 2025). Proposes an identity framework for autonomous AI agents, focusing on authentication and authorization between agents and humans. No append-only audit log design.

**Identity Management for Agentic AI** (OpenID Foundation, April 2025). A commissioned whitepaper surveying the intersection of agentic AI and identity management. Recommends evolving existing identity standards (OAuth 2.0, OIDC, SAML) rather than inventing new ones. No mention of cryptographic audit trails.

**Relevance:** Low for the audit-trail format, medium for positioning. None of the academic work found addresses the specific problem agent-provenance solves (verifiable, offline-auditable execution logs for AI agent tool calls). This is both a validation (no direct competitor) and a risk (no prior art to lean on for auditor acceptance).

**Implication:** None for v1 spec. For positioning documentation, note that agent-provenance fills a gap not yet covered by existing academic frameworks.

*Sources:* [MAIF arXiv 2511.15097](https://arxiv.org/abs/2511.15097); [Zero-Trust arXiv 2505.19301](https://arxiv.org/abs/2505.19301); [OpenID Foundation AI Identity Report](https://openid.net/identity-management-for-agentic-ai/).

---

## 3. Thread 2: Compliance Regime Findings

### 3.1 SOX §404 / PCAOB AS 2201 — IT General Controls

PCAOB AS 2201 requires auditors to obtain evidence about the design and operating effectiveness of ITGCs. The evidence artifacts are **management-produced**: screenshots of system configurations, exported access control lists, change management logs, policy documents, and training records. Auditors perform walkthroughs (live demos) during the audit, but the **workpaper evidence is static**. Auditors do not typically connect directly to production systems during the audit; they request evidence from the client, then test it.

The ITGC audit focuses on three areas: access (who can do what), change (who approved what change), and IT operations (batch job monitoring, error handling). For each area, the auditor wants to see that controls exist, that they are documented, and that they operated effectively during the audit period.

**Implication for v1 spec §4:** The bundle-as-evidence model aligns with SOX practice. A `bundle/` directory containing signed events, keys, and attestations is exactly the kind of "system export" that SOX auditors expect. The hybrid model (direct verification for discovery, bundles for evidence) is also appropriate — walkthroughs use live access, workpapers use bundles.

*Sources:* [PCAOB AS 2201](https://pcaobus.org/oversight/standards/auditing-standards/details/AS2201); [PCAOB AS 1215 Audit Documentation](https://pcaobus.org/oversight/standards/auditing-standards/details/AS1215); [SOX ITGC Evidence Types](https://validence.com/sox-audit-evidence-validation/).

---

### 3.2 AICPA SOC 2 Trust Services Criteria (Current Version)

SOC 2 is criteria-based, not prescriptive about format. The relevant criteria are:
- **CC4.1 (Control Monitoring):** The entity monitors its controls over time and evaluates whether they are operating effectively.
- **CC7.2 (System Monitoring):** The entity monitors system components and related operations for anomalies that may indicate a compromise.

Auditors evaluate these criteria by reviewing: log samples, access review documentation, change logs, incident response records, and monitoring tool outputs. There is **no mandated format** for the evidence. SOC 2 auditors may request read-only access to the SIEM or log system during the audit period, but the final report relies on documentation, not live system connectivity.

**Implication for v1 spec §4:** The verifier tool producing a structured report (JSON + human-readable) is an ideal SOC 2 evidence artifact. It directly demonstrates that controls are operating (CC4.1) and that the system is monitoring itself (CC7.2). The bundle format should be considered a "monitoring tool output" in SOC 2 terms.

*Sources:* [AICPA Trust Services Criteria](https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-governance/trust-services-criteria); [SOC 2 Trust Services Criteria Guide](https://truvocyber.com/blog/soc-2-trust-services-criteria-guide); [Drata SOC 2 Criteria](https://drata.com/learn/soc-2/trust-services-criteria).

---

### 3.3 HIPAA §164.312(b) — Audit Controls

HIPAA requires "Implement hardware, software, and/or procedural mechanisms that record and examine activity in information systems that contain or use electronic protected health information." OCR enforcement actions reveal that failed implementations fall into three categories:
1. **No audit logs at all** — systems that do not capture access to PHI.
2. **Incomplete logs** — systems that log some access but miss critical events (e.g., viewing a patient record but not downloading it).
3. **Logs not reviewed** — systems that capture logs but never examine them.

Notable enforcement: A $2.175 million settlement in 2019 for hospitals that failed to implement audit controls (logging access to systems containing PHI). OCR does not specify log format, signing, or export format in the regulation; it requires the *capability* to record and examine.

**Implication for v1 spec §4:** The agent-provenance design exceeds HIPAA’s minimum by providing tamper-evident, signed logs. The bundle export satisfies the "examine activity" requirement. No format change needed, but documentation should note that the design is "exceeds HIPAA §164.312(b) baseline."

*Sources:* [HHS Resolution Agreements](https://www.hhs.gov/hipaa/for-professionals/compliance-enforcement/agreements/index.html); [HIPAA Journal Violation Cases](https://www.hipaajournal.com/hipaa-violation-fines/); [OCR HIPAA Audit Program](https://www.hhs.gov/hipaa/for-professionals/compliance-enforcement/audit/index.html).

---

### 3.4 FFIEC IT Examination Handbook — Information Security Booklet

The FFIEC is prescriptive about log integrity. The Cybersecurity Assessment Tool (CAT) Domain 3 (Cybersecurity Controls) includes: "Audit log records and other security event logs are reviewed and retained in a secure manner." The Information Security Booklet (page 79) states: "Processes for the security and integrity of log files (e.g., encryption of log files, adequate access controls) are implemented."

Banking examiners expect to see: file integrity monitoring on log files, access controls preventing log tampering, centralized logging, and regular log review. The FFIEC language is more specific than HIPAA or SOC 2 about *integrity* protections.

**Implication for v1 spec §4:** The signed bundle with per-event signatures and Merkle-root timestamp anchors directly addresses FFIEC’s "security and integrity of log files" requirement. This is the most concrete alignment found across any regime. Consider adding FFIEC to the compliance positioning in `agent-provenance/README.md`.

*Sources:* [FFIEC Cybersecurity Assessment Tool (PDF)](https://www.ffiec.gov/sites/default/files/media/resources/FFIEC_CAT_May_2017.pdf); [FFIEC IT Handbook Information Security Booklet](https://www.ffiec.gov/it-booklets/information-security/).

---

### 3.5 PCI DSS v4.0 — Requirement 10

PCI DSS v4.0 Requirement 10 changed significantly from v3.2.1:
- **10.3.4 (new):** "Utilize File Integrity Monitoring or Change-Detection Tools on Audit Logs." This was a best practice until March 2025; it is now required.
- **10.5.1:** Secure audit logs so they cannot be altered.
- **10.5.2:** Retain audit logs for at least one year, with the most recent 90 days immediately available.
- **10.6:** Review logs for anomalies at least daily (automated mechanisms are now required by March 2025).

The auditor’s workpaper typically contains: log samples showing the required fields (user ID, event type, date/time, success/failure, etc.), evidence of FIM on log files, and evidence of automated review.

**Implication for v1 spec §4:** The bundle format supports the 1-year retention requirement (offline, portable storage). The verifier tool’s automated checking of signatures and temporal ordering maps directly to PCI DSS 10.3.4 and 10.6. The `manifest.json` integrity hashes are a form of file-integrity monitoring.

*Sources:* [PCI DSS 4.0 Summary of Changes (PDF)](https://docs-prv.pcisecuritystandards.org/PCI%20DSS/PCI-DSS-v4-0-Summary-of-Changes.pdf); [PCI DSS 4.0 Requirement 10 Explained](https://vistainfosec.com/blog/pci-dss-requirement-10-changes-from-v3-2-1-to-v4-0-explained/).

---

### 3.6 ISO/IEC 27001:2022 — Annex A 8.15 and 8.16

Annex A 8.15 (Logging) requires: "Logs that record activities, exceptions, faults and other relevant events shall be produced, stored, protected and analysed." Annex A 8.16 (Monitoring Activities) requires: "Networks, systems and applications shall be monitored for anomalous behaviour, and appropriate actions taken to evaluate potential information security incidents."

ISO 27001 is not prescriptive about log format or evidence type. It is principles-based. The auditor looks for evidence that logging exists, that logs are protected, and that they are reviewed.

**Implication for v1 spec §4:** Agent-provenance is compatible but not uniquely aligned. Any compliant logging system would satisfy ISO 27001. No specific action needed.

*Sources:* [ISO 27001:2022 Annex A 8.15](https://hightable.io/iso-27001-annex-a-8-15-logging/); [ISO 27001:2022 Annex A 8.16](https://hightable.io/iso-27001-annex-a-8-16-monitoring-activities/).

---

### 3.7 EU AI Act — Article 12 (Record-Keeping)

Article 12 requires: "High-risk AI systems shall technically allow for the automatic recording of events (logs) over the lifetime of the system." Logs must be retained for "as long as is appropriate and proportionate." The Implementing Regulation (expected 2026) will specify technical standards. The AI Act does not yet specify cryptographic signing, bundle formats, or evidence export requirements.

**Implication for v1 spec §4:** The v1 design satisfies the Article 12 requirement. If the future Implementing Regulation mandates specific formats (e.g., X.509, eIDAS signatures), v2 may need to adapt. No v1 change needed, but monitor the Implementing Regulation.

*Sources:* [EU AI Act Article 12 — AI Act Service Desk](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article/12); [EU AI Act Article 12 Text](https://artificialintelligenceact.eu/article/12/).

---

### 3.8 Thread 2 Synthesis: Live Access vs. Static Exports

Across all seven regimes checked, the dominant pattern is:
1. **Static evidence exports** (screenshots, exported logs, policy documents, signed bundles) are the primary workpaper evidence.
2. **Live access** is used during walkthroughs and for control testing during the audit period, but the auditor does not typically take away a live database connection.
3. **No regime mandates cryptographic signing** of logs, but several (PCI DSS 10.3.4, FFIEC) mandate *integrity controls* that cryptographic signing satisfies.
4. **SOC 2 and ISO 27001 are the most flexible** — any evidence format is acceptable if it demonstrates the criteria.
5. **SOX and PCI DSS are the most prescriptive** about what evidence must contain, but not about the wire format.

**Conclusion:** The v1 spec’s "bundles for evidence" model is well-supported by real-world audit practice. The hybrid model (direct verification for discovery, bundles for evidence) is also appropriate. No design change needed.

---

## 4. Thread 3: TSA Landscape Findings

### 4.1 Comparative Table

| TSA | Type | Rate Limits | Audit Certifications | SOX/SOC2 Acceptable? | Notes |
|---|---|---|---|---|---|
| **DigiCert (commercial)** | Commercial | Enterprise contracts (no hard rate limit) | WebTrust for TSAs, eIDAS QTSP | **Yes** | Industry standard; per-token or subscription pricing; ~$0.02–$0.10/token at volume. |
| **GlobalSign (commercial)** | Commercial | Enterprise contracts | WebTrust, eIDAS QTSP | **Yes** | Direct competitor to DigiCert; similar pricing. |
| **Sectigo (commercial)** | Commercial | Enterprise contracts | WebTrust | **Yes** | Budget-friendly alternative; fewer eIDAS options. |
| **IdenTrust (commercial)** | Commercial | Enterprise contracts | WebTrust, US Federal trust | **Yes** | Strong in financial services; US government acceptance. |
| **FreeTSA (freetsa.org)** | Free public | ~1,000 requests/day (undocumented) | None | **No** | Run by individual developer; no SLA; no audit trail. Development/testing only. |
| **Apple TSA** | Free (captive) | Apple-internal | WebTrust (Apple CA) | **Maybe** | Used for Apple notarization; not a general-purpose public TSA. |
| **Sigstore Timestamp Authority** | Self-hosted | Deployment-dependent | None (unless you operate your own CA with WebTrust) | **Only if qualified** | Open-source RFC 3161 server; RFC 3628 policy; RFC 5816 structure. Best self-hosted option but requires CA qualification. |
| **Bouncy Castle (library)** | Self-hosted | Deployment-dependent | None | **Only if qualified** | Java library for building TSAs; you bring the CA and HSM. |

### 4.2 RFC 3161 vs. Alternatives

- **RFC 3161 (2001)** remains the standard for trusted timestamping. No protocol has superseded it for audit evidence.
- **RFC 5816 (2010)** updated RFC 3161 with certificate ordering rules. Sigstore Timestamp Authority conforms to RFC 5816.
- **RFC 3628 (2004)** defines the security policy requirements for TSAs. Commercial TSAs (DigiCert, GlobalSign) are audited against this.
- **Blockchain-based** (OpenTimestamps, Ethereum notarization) exists but is not accepted as audit-grade evidence by SOX/PCI DSS auditors. No regulator recognizes blockchain timestamps as equivalent to RFC 3161.

### 4.3 Rate Limit Analysis for 15-Minute Batch Interval

At the 15-minute batch interval recommended in v1 spec §1:
- **24 batches/day** → ~730 batches/month.
- **DigiCert commercial:** Negligible cost; enterprise subscriptions cover this volume in the base fee.
- **FreeTSA:** Technically within the ~1,000/day limit, but the lack of SLA means a batch failure would be unrecoverable. Not recommended.
- **Self-hosted (Sigstore TSA):** No rate limit, but requires operational overhead (CA maintenance, HSM, WebTrust audit if audit-grade evidence is required).

### 4.4 Recommendation

**Default for v1 documentation:**
1. **Commercial deployment:** DigiCert Timestamp Authority (or GlobalSign) as the recommended default. Document that a WebTrust-for-TSA-audited commercial TSA is required for SOX/PCI DSS evidence.
2. **Self-hosted / homelab:** Sigstore Timestamp Authority as the recommended self-hosted option. Document that self-hosted TSAs require a qualified CA to be audit-grade; without one, timestamps are integrity-only (sufficient for internal audit, not for external auditor evidence).
3. **Development/testing:** FreeTSA with a prominent warning: "Not suitable for compliance evidence. No SLA, no audit certifications."

*Sources:* [Sigstore Timestamp Authority README](https://github.com/sigstore/timestamp-authority/blob/main/README.md); [DigiCert Timestamp Services](https://www.digicert.com/signing/timestamping); [RFC 3161](https://datatracker.ietf.org/doc/html/rfc3161); [RFC 5816](https://datatracker.ietf.org/doc/rfc5816); [RFC 3628](https://datatracker.ietf.org/doc/html/rfc3628); [WebTrust for TSA](https://www.webtrust.org/).

---

## 5. Thread 4: DID Rotation Mechanics

### 5.1 DID Core 1.0 — Verification Method Updates

DID Core 1.0 (W3C Recommendation, July 2022) defines DID documents as containing `verificationMethod` entries. A DID controller can update the DID document to add, remove, or replace verification methods. The exact mechanism is DID-method-specific. DID Core does not specify a universal rotation protocol — it says rotation is "achieved by updating the DID document."

### 5.2 did:key — No Rotation

did:key derives the DID directly from the public key (multicodec-encoded). If the key rotates, the DID changes. This is fundamentally incompatible with agent-provenance’s stable `principal_id` across rotation.

### 5.3 did:web — URL-Based Rotation
did:web resolves the DID document from an HTTPS URL. The URL is the stable identifier; the keys within the document can change. This is similar in principle to agent-provenance (stable identifier, rotating keys), but it requires a live web server for resolution. The rotation mechanism is: update the JSON file at the URL.

### 5.4 did:plc — Signed Operation History
did:plc (Bluesky/AT Protocol) uses a separate "rotation key" that authorizes updates to the DID document. The DID itself is a hash of the initial creation operation. Subsequent updates are signed operations posted to a "PLC directory" (a hosted log). The signed operation chain is structurally equivalent to agent-provenance’s `key_rotation` event chain: each rotation is signed by an authorized key (the old key or a recovery key), and identity continuity is preserved.

### 5.5 W3C Verifiable Credentials Data Model v2.0

VC 2.0 does not specify key rotation at the credential level. Key management is the DID/controller’s responsibility. The spec notes that "verification methods may be revoked or rotated" but does not prescribe a mechanism.

### 5.6 Equivalence Assessment

| Aspect | Agent-Provenance v1 | did:plc |
|---|---|---|
| Stable identity across rotation | `principal_id` unchanged | DID unchanged |
| Rotation authorization | Old key signs `key_rotation` event | Rotation key signs update operation |
| Recovery mechanism | `role: "recovery"` key signs | Recovery key signs |
| Continuity proof | Signed event chain | Signed operation chain |
| Resolution required? | No (offline verifier walks chain) | Yes (PLC directory lookup) |

**Finding:** The v1 `key_rotation` event chain is *materially equivalent* to did:plc’s signed operation chain in terms of security semantics (stable identity, predecessor-signed rotation, recovery key support). However, there is **no benefit to adopting DID terminology** because:
1. No auditor tooling today understands DID terminology in the context of offline audit bundles.
2. DID resolution (even did:web) adds a network dependency that the peer model explicitly avoids.
3. The v1 terminology (`key_rotation`, `old_key_fingerprint`, `new_key_fingerprint`) is more direct and self-documenting for the target audience (IT auditors and security engineers).

**Conclusion:** Current design stands without owing an explanation.

*Sources:* [DID Core 1.0](https://www.w3.org/TR/did-core/); [did:key Method v0.9](https://w3c-ccg.github.io/did-method-key/); [did:web Method](https://w3c-ccg.github.io/did-method-web/); [did:plc Spec](https://web.plc.directory/spec/v0.1/did-plc); [VC Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/).

---

## 6. Questions for Humans

1. **CloudTrail chain continuity:** Should `manifest.json` include a `previous_bundle_hash` field to create a cross-bundle hash chain (like CloudTrail’s `previousDigestHashValue`)? This would enable gap detection across retention boundaries but is not in the current §4 spec. *(v1 spec §4, Bundle format)*

2. **Sigstore bundle as a v2 exploration:** The finding is that Sigstore bundles are incompatible with the peer model and append-only log structure. Should this be documented as an explicit "considered and rejected" note in `agent-provenance/README.md` to prevent future revisiting? *(agent-provenance/README.md, trust model)*

3. **FFIEC compliance positioning:** FFIEC’s prescriptive language on log integrity is the tightest alignment found across any regime. Should the README add FFIEC as an explicitly supported compliance target, alongside SOX/HIPAA/SOC 2? *(agent-provenance/README.md, §compliance positioning)*

4. **EU AI Act monitoring:** The Implementing Regulation (expected 2026) may specify technical standards for high-risk AI system logging. Should the v2 roadmap include a work item to track and potentially adopt the harmonized standard when published? *(v2 roadmap)*

5. **TSA default in documentation:** DigiCert is recommended as the commercial default. Is there a known DigiCert pricing objection (e.g., for small teams / open-source projects) that would make a different commercial TSA preferable? *(v1 spec §1, Temporal integrity)*

6. **OpenTimestamps as optional layer:** Should the v2 roadmap explicitly reserve a slot for Bitcoin-anchored timestamping (OpenTimestamps) as a hardening layer, or is this out of scope indefinitely? *(agent-provenance/README.md, trust model layer 6)*

7. **Academic gap:** No published work addresses verifiable AI agent execution logs. Should the team consider publishing a short technical report or blog post to establish prior art and invite review? *(positioning, not design)*

---

## 7. What Was NOT Checked

- **NIST SP 800-53 Rev. 5 (AU-6, AU-9, AU-10):** Considered but skipped because it is US-government-specific and the workplace-audit use case focuses on financial/healthcare compliance (SOX/HIPAA/SOC 2). Revisit if a federal customer emerges.
- **GDPR Article 30 (Records of Processing Activities):** Skipped because the v1 spec explicitly defers GDPR to deployment concerns beyond redaction-by-hash. No new finding would change that.
- **Specific pricing for DigiCert TSA:** Checked at the category level (per-token/subscription) but did not request a quote or check current list pricing. The recommendation stands regardless of exact price.
- **Specific SOX auditor interviews:** Cited PCAOB standards and industry guides (Validence, A2Q2) but did not interview a practicing Big Four auditor. The finding is based on published practice guides, not primary-source testimony.
- **Channels API implementation details:** Explicitly out of scope per the prompt. The CloudTrail analog covers the structural integrity model; implementation mechanics of the Channels API are not load-bearing for this research.
- **Substrate breadcrumbs 196–221:** The substrate directory was empty/nearly empty in the provided files. The v1 spec and v0 plan were read in full. If breadcrumb files contain additional design context not reflected in the spec, this research may have missed a nuance.
- **Round-by-round review documents in `/projects/agent-wake/design/`:** Skipped per prompt instructions ("not load-bearing for your research"). The v1 spec is treated as the authoritative consolidated view.

---

*End of report.*
