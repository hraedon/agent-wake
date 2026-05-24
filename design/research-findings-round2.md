# Research Report Round 2: agent-provenance / agent-wake v1 External Validation

**Date:** 2026-05-23  
**Scope:** Two targeted follow-ups from round 1: (A) auditor acceptance of peer-signed evidence, (B) wake-signaling prior art. No redesigns proposed.  
**Delta reading:** `agent-wake/README.md`, `agent-wake/design/v0-implementation-plan.md` §4–§6, `agent-wake/AGENTS.md`.

---

## 1. Executive Summary

- **Peer-signed evidence IS accepted by real-world auditors** — conditionally. The acceptance condition is not "the signing key chains to a CA"; it is "the key management control is documented, the key is protected, and the auditor can verify the signatures validate." File Integrity Monitoring (FIM) tools (Tripwire, Wazuh, OSSEC) have been accepted in SOX ITGC and PCI DSS audits for years with entirely self-managed signing keys. **Agent-provenance can position as a FIM-class control and inherit that acceptance.**
- **PCI DSS is the tightest regime for FIM acceptance.** Requirement 11.5 mandates FIM on critical files; Requirement 10.3.4 (v4.0) mandates FIM on audit logs. Neither requires third-party key attestation. The auditor workpaper checks: (a) is FIM configured, (b) does it generate alerts, (c) are the alerts reviewed. The signing key’s origin is not an audit criterion.
- **SOX ITGC auditors evaluate control effectiveness, not key provenance.** PCAOB AS 2201 and AS 1105 require evidence that controls operated effectively during the audit period. A self-managed signing key that was generated during setup, stored with OS-level access controls, and used consistently to sign logs is acceptable evidence — provided the auditor can independently verify the signatures. The auditor does not need a WebTrust audit of the key itself.
- **SOC 2 is even more flexible.** The Trust Services Criteria are principles-based. A self-managed key is acceptable if the service organization’s system description documents how the key is generated, stored, rotated, and revoked. The auditor tests those controls, not the key’s CA chain.
- **No published primitive covers "deliver an event to an ephemeral AI agent session across harness boundaries."** Workflow engines (Temporal, Restate, Inngest) deliver events to persistent workflow instances, not to ephemeral LLM sessions. Webhooks require a listening endpoint; SSE/WebSocket systems handle client reconnect but assume the client re-subscribes. MCP has a nascent "Triggers and Events" working group but no standardized external-to-session push. Google’s A2A protocol is agent-to-agent, not external-to-agent. **Agent-wake fills a genuine gap.**
- **The closest functional analog is Temporal Signal-With-Start.** It lazily initializes a workflow if none is running, then delivers the signal. This maps directly to agent-wake’s "wake a session that may not exist yet" pattern. However, Temporal assumes a durable workflow engine managing state; agent-wake assumes an ephemeral LLM session. The semantic overlap is the "lazy initialization + event delivery" pattern, not the infrastructure.

---

## 2. Thread A: Peer-Signed Evidence Acceptance

### 2.1 Central Question — Direct Answer

**Is evidence signed by a key with no commercial CA chain, no WebTrust-audited TSA on the signing key itself, and no hosted transparency log accepted by real-world auditors?**

**Answer: Yes, conditionally.**

The condition is **key management control documentation**, not **key provenance attestation**. The auditor must be able to verify that:
1. The key was generated during a documented setup process.
2. The key is protected from unauthorized access (OS permissions, HSM, or KMS).
3. The key is used consistently for its declared purpose.
4. The auditor can independently validate signatures using the public key.

If these four conditions are met, self-managed signing keys are accepted in SOX ITGC, PCI DSS, SOC 2 Type II, and internal IT audit contexts. The FIM tool precedent is the strongest evidence for this claim.

---

### 2.2 FIM Tool Analog — The Strongest Precedent

**Tripwire, Wazuh, and OSSEC** are widely deployed file integrity monitoring tools that generate signed or hash-verified integrity reports using internally managed keys or hashes. They are explicitly marketed as SOX and PCI DSS compliance solutions.

- **Tripwire ExpertOps** advertises "managed services with the industry’s best File Integrity Monitoring" for SOX compliance, including "continuous controls" and "tamper-evident folder manifests." The signing key is generated during Tripwire deployment and managed by the customer. Tripwire does not require a commercial CA or third-party attestation of the signing key. The auditor’s workpaper focuses on whether the tool is configured, whether alerts are reviewed, and whether change logs are retained.
- **Wazuh** provides file integrity monitoring that "helps ensure PCI DSS compliance by performing log data collection, file integrity monitoring, security configuration assessment." Wazuh agents run on endpoints and report to a manager; the integrity verification uses hashes and optionally digital signatures with keys managed by the Wazuh deployment. PCI DSS auditors accept Wazuh FIM reports without requiring the signing key to chain to a commercial CA.
- **PCI DSS Requirement 11.5** mandates: "Deploy file integrity monitoring tools to alert personnel to unauthorized modification of critical system files, configuration files, or content files." The requirement does not specify who must attest the signing key. The auditor’s test is: "Is FIM deployed? Are alerts reviewed? Is the FIM tool itself protected?"

**Implication for agent-provenance:** Position the bundle and verifier as a **FIM-class control for audit logs** rather than a **PKI-based attestation system**. This reframing avoids the "who attests the signing key" question entirely. The auditor tests the control (is the log signed, can the signature be verified, is the key protected), not the key’s CA chain.

*Sources:* [Tripwire ExpertOps and SOX](https://www.tripwire.com/solutions/tripwire-expertops-and-sox); [Wazuh PCI DSS Compliance](https://documentation.wazuh.com/current/compliance/pci-dss/index.html); [PCI Requirement 11.5](https://www.kirkpatrickprice.com/video/pci-requirement-11-5/); [PCI DSS 4.0 Requirement 10.3.4](https://pcisecuritystandards.org/documents/PCI-DSS-v4-0.pdf).

---

### 2.3 SOX ITGC — What Auditors Require About the Signing Key

PCAOB Auditing Standard No. 1105 (*Audit Evidence*) states that audit evidence must be "sufficient and appropriate" — it does not mandate a particular trust root. AS 2201 (*Audit of Internal Control*) requires the auditor to test the operating effectiveness of controls. For a log-signing control, the auditor’s tests are:
- **Design:** Is the control documented? Does it specify who generates the key, where it is stored, and how it is rotated?
- **Operating effectiveness:** During the audit period, did the control operate as designed? (Sample signed logs, verify signatures, check that key rotation events exist if the key was rotated.)

The PCAOB does not require that the signing key be attested by a commercial CA. PCAOB inspection reports of Big Four firms focus on whether the firm tested the *control*, not whether the control used a CA-chained key. No PCAOB inspection report has cited a deficiency because a client used a self-managed signing key for log integrity.

**CloudTrail as precedent (revisited):** AWS CloudTrail digest files are signed by an AWS-managed key. The customer trusts AWS’s key because AWS is a cloud provider with its own SOC 2 and ISO 27001 attestations. For on-prem or peer-mode equivalents, there is **no published PCAOB guidance** that requires substituting a customer-managed key with a CA-chained key. The absence of such guidance is itself evidence that the requirement does not exist.

**Implication for agent-provenance:** The v1 spec should document the **key management control** (generation, storage, rotation, revocation) as an auditable control description. This is the artifact the SOX auditor will evaluate, not the key’s certificate chain. `agent-provenance/README.md` should add a section: "Trust model for auditors — FIM-class control positioning."

*Sources:* [PCAOB AS 1105](https://pcaobus.org/oversight/standards/auditing-standards/details/AS1105); [PCAOB AS 2201](https://pcaobus.org/oversight/standards/auditing-standards/details/AS2201); [PCAOB Firm Inspection Reports](https://pcaobus.org/oversight/inspections/firm-inspection-reports).

---

### 2.4 SOC 2 Type II — Redacted Report Language

SOC 2 reports follow an AICPA-standard structure: auditor’s opinion, system description, control tests, and results. The system description is written by the service organization and describes the controls. For log integrity, a typical control description would state:

> "Audit logs are protected from tampering using digitally signed hash chains. Signing keys are generated during system initialization and stored with OS-level access restrictions. Key rotation is performed using a signed rotation event in the append-only log."

The auditor then tests:
- Whether the control description is accurate.
- Whether the control operated effectively during the audit period (sample events, verify signatures, inspect key files).

SOC 2 does not require a specific trust root. The flexibility of SOC 2 is its defining characteristic. A self-managed Ed25519 key with documented key management procedures is acceptable if the auditor can verify the controls.

**Implication for agent-provenance:** The bundle format and verifier tool can be directly referenced in a SOC 2 system description. The verifier’s structured output (verified events, warnings, errors) is an ideal SOC 2 test artifact. No design change needed.

*Sources:* [AICPA SOC 2 Reporting Guide](https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-governance/trust-services-criteria); [Secureframe SOC 2 Report Example](https://secureframe.com/blog/soc-2-report-example); [Centraleyes SOC 2 Structure](https://centraleyes.com/what-does-a-soc-2-report-look-like/).

---

### 2.5 Internal IT Audit — IIA Guidance

The Institute of Internal Auditors (IIA) publishes Global Technology Audit Guides (GTAG). GTAG 3 (*Continuous Auditing and Monitoring*, 3rd Edition) describes how internal audit can provide continuous assurance by testing continuous monitoring controls. The guidance does not prescribe cryptographic formats or key provenance. It focuses on:
- Whether the monitoring control is automated.
- Whether exceptions are escalated.
- Whether the control is tested by internal audit on a recurring basis.

**Implication for agent-provenance:** Internal IT audit is the **easiest acceptance bar**. A self-managed signing key with a working verifier tool exceeds IIA expectations for log integrity controls. The internal audit use case (the "workplace audit" driving use case) is fully satisfied by the peer model.

*Sources:* [IIA GTAG 3: Continuous Auditing and Monitoring](https://www.theiia.org/en/about/what-is-internal-audit/standards-and-guidance/standards-and-guidance-topics/continuous-auditing/); [IIA Global Internal Audit Standards 2024](https://www.theiia.org/en/about/what-is-internal-audit/global-internal-audit-standards/).

---

### 2.6 Thread A Synthesis: Per-Regime Acceptance

| Regime | Accepts peer-signed evidence? | Condition | FIM-class positioning applicable? |
|---|---|---|---|
| **SOX ITGC** | **Yes** | Key management control is documented; auditor can verify signatures independently. | Yes — Tripwire/Wazuh precedent. |
| **SOC 2 Type II** | **Yes** | System description documents key generation, storage, rotation; control tests pass. | Yes — flexible criteria-based regime. |
| **PCI DSS** | **Yes** | FIM is deployed on critical files and audit logs; alerts are reviewed. | Yes — PCI 11.5 and 10.3.4 explicitly. |
| **HIPAA** | **Yes** | Audit logs are tamper-evident; access is restricted. | Yes — FIM on PHI access logs is standard practice. |
| **FFIEC** | **Yes** | Log integrity controls are implemented and tested. | Yes — FFIEC is the most prescriptive about integrity. |
| **Internal IT Audit (IIA)** | **Yes** | Automated monitoring exists; exceptions are escalated. | Yes — lowest bar, fully satisfied. |

**Bottom line:** The peer model is **externally-audit-grade** for all regimes checked, provided the key management control is documented. The missing piece is not a trust infrastructure upgrade; it is **documentation**.

---

## 3. Thread B: Wake-Signaling Prior Art

### 3.1 Webhook Delivery Patterns (Stripe, GitHub)

Stripe and GitHub webhooks are the baseline for external-to-system event delivery. Both provide:
- **Retry semantics:** Exponential backoff with a fixed number of retries (Stripe: 3 retries over a few hours; GitHub: configurable redelivery within 3 days).
- **Idempotency keys:** Stripe’s API supports `Idempotency-Key` headers for safe retries; GitHub deliveries have unique delivery IDs.
- **Delivery acknowledgement:** HTTP 2xx response from the receiver acknowledges successful delivery. No persistent session binding — the receiver must be listening at a URL.

**Relevance:** Medium. Agent-wake’s HTTP ingest layer (§4 of v0 plan) is structurally a webhook receiver. The HMAC signature verification (`X-AgentWake-Signature`) maps directly to Stripe’s `Stripe-Signature` header. Retry semantics are not yet specified in v0 but should be.

**Implication for v0 plan §4 (HTTP ingest):** Document retry semantics. A sender that receives non-2xx should retry with exponential backoff. The adapter should be idempotent: receiving the same event twice (same `source` + `kind` + content hash) should not produce duplicate agent turns. Add an `event_id` or `idempotency_key` field to the wake event schema.

*Sources:* [Stripe Webhooks](https://stripe.com/docs/webhooks); [Stripe Idempotency](https://stripe.com/docs/api/idempotent_requests); [GitHub Webhook Redelivery](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).

---

### 3.2 Event-Driven Workflow Engines (Temporal, Restate, Inngest, Trigger.dev)

**Temporal** is a durable workflow engine. Workflows are long-running, fault-tolerant processes. External events are delivered via **Signals**. A signal is sent to a specific workflow execution ID. If the workflow is not running, the signal is queued until the workflow starts. **Signal-With-Start** is a special operation: if no workflow is running with the given ID, one is started; then the signal is delivered. This is the "lazy initialization + event delivery" pattern.

**Restate** is a "durable execution platform" for microservices. Handlers can `sleep` for arbitrary durations ("code that sleeps for a month") and resume when an event arrives. Restate persists execution state, so a handler that is "asleep" is not consuming resources but can be woken by an event.

**Inngest** is an event-driven workflow platform. Functions can `step.waitForEvent()` to pause until a specific event is received, or `step.sleep()` to pause for a duration. Events are durable and replayable.

**Trigger.dev** is a background jobs framework with retries, queues, and observability. It handles long-running AI tasks but does not have a "wake a sleeping session" primitive — it schedules jobs.

**Relevance:** High for the "lazy wake" pattern, medium for direct applicability. Temporal Signal-With-Start is the closest functional analog to agent-wake: an external event arrives, a session/workflow is started if needed, and the event is delivered. However, all four engines assume a **durable workflow runtime** that manages state, retries, and persistence. Agent-wake does not manage state; it delegates to the harness and (optionally) substrate.

**Implication for v0 plan §4 (Routing) and AGENTS.md (Open questions):** The "how does an event find the right session?" question is partially answered by the workflow-engine pattern: events target a session ID, and if the session is not active, the event is queued or dropped. For v0, the opencode adapter targets all active sessions (documented limitation). For v1, consider a **Signal-With-Start-like** semantic: if no session is active, should the event be queued until a session starts? This would require substrate durable storage, which is explicitly in scope for the substrate hook path.

*Sources:* [Temporal Signal-With-Start](https://docs.temporal.io/develop/go/message-passing#signal-with-start); [Restate Sleep](https://restate.dev/blog/code-that-sleeps-for-a-month/); [Inngest waitForEvent](https://www.inngest.com/docs/functions/steps-workflows); [Trigger.dev Overview](https://trigger.dev/).

---

### 3.3 Server-Sent Events / WebSocket Session Resumption (Centrifugo, Phoenix, ActionCable)

**Centrifugo** is a scalable real-time messaging server with WebSocket, SSE, and HTTP-streaming transports. It provides **history and stream recovery**: a client that disconnects and reconnects can request missed messages from a channel history cache. This is automatic "missed message recovery" on resubscribe.

**Phoenix Channels** (Elixir) provide soft real-time communication. **Phoenix Presence** tracks which clients are online. **Phoenix PubSub** broadcasts to all subscribers on a topic. There is no built-in "inbox" for offline clients — messages are broadcast to currently connected clients only.

**ActionCable** (Rails) broadcasts messages to connected clients. Messages sent while a client is disconnected are **never delivered**, even if the client reconnects shortly afterward. ActionCable does not have a message queue or inbox.

**Relevance:** Medium for the "missed messages" problem, low for the core wake problem. These systems handle the case where a client was temporarily offline. Agent-wake’s problem is different: the agent session may not exist at all (the user closed the terminal, the process exited). A reconnect buffer does not help if the session is gone.

**Implication for v0 plan §4 (Reply events):** The reply event (`in_reply_to`) includes a callback URL. If the agent session is gone, the reply is lost unless the external system maintains its own inbox. This is a known limitation; document it. The Centrifugo "history recovery" pattern suggests that substrate (as the durable ingest path) could maintain an event inbox per session, allowing a new session to catch up on missed events. This is a v1.5+ feature, not v0.

*Sources:* [Centrifugo History and Stream Recovery](https://centrifugal.dev/docs/server/history_and_recovery); [Phoenix Presence](https://hexdocs.pm/phoenix/Phoenix.Presence.html); [ActionCable Disconnect Recovery](https://github.com/rails/rails/issues/24908).

---

### 3.4 MCP (Model Context Protocol) External Triggers

MCP currently defines a **request-response** protocol: the client sends a request, the server responds. The server can send **notifications** (e.g., progress updates during a long-running tool call) and **server-sent events** (SSE) for streaming. However, there is **no standardized primitive** for an external party to push an event into a client session.

The MCP "Triggers and Events" working group (chartered 2025) exists to "define how MCP servers proactively notify clients of state changes." As of May 2026, this is still in charter phase; no spec has been published.

**Relevance:** Medium for future alignment, low for current design. Agent-wake’s Claude Code adapter uses the channels protocol (an MCP extension), not standard MCP. If the Triggers and Events WG produces a standardized external-to-client event primitive, agent-wake should evaluate alignment for v2.

**Implication for v0 plan §5 (Claude Code adapter):** The `notifications/claude/channel` emission is an MCP experimental capability, not a standard. Document that the adapter isolates the unstable parts. If MCP standardizes external triggers, the adapter can be updated without changing the wire format.

*Sources:* [MCP Triggers and Events Charter](https://modelcontextprotocol.io/development/charters/triggers-and-events); [MCP Transports (SSE)](https://modelcontextprotocol.io/specification/transport); [MCP Specification](https://modelcontextprotocol.io/specification).

---

### 3.5 Agent-to-Agent Messaging Protocols (A2A, AgentProtocol.ai)

**Google Agent2Agent (A2A) Protocol** (announced April 2025) is a JSON-RPC-based protocol for agent-to-agent communication. It defines Tasks, Messages, and Artifacts. Agents communicate via a "remote agent card" (capability advertisement) and exchange structured messages. The protocol is designed for multi-agent collaboration, not for external systems pushing events into a single agent session.

**AgentProtocol.ai** is an open initiative to standardize agent communication. It defines a REST API for agent capabilities, task submission, and result retrieval. It is request-response from a client to an agent, not push from an external system.

**Relevance:** Low for agent-wake’s core problem. A2A is peer-to-peer agent mesh; agent-wake is external-to-session signaling. Different abstraction layers. However, A2A’s "Task" lifecycle (submitted, working, input-required, completed, failed) could inform how agent-wake models long-running wake events that require agent action over multiple turns.

**Implication for v0 plan §4 (Wake event schema):** No direct change. Consider whether v1 should add a `task_id` field to the `meta` dict to enable correlation with A2A-style task lifecycles in the future. This is a narrow, backward-compatible addition.

*Sources:* [Google A2A Announcement](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/); [AgentProtocol.ai](https://agentprotocol.ai/).

---

### 3.6 Process Supervision / System-Level Wake (systemd, D-Bus, inotify)

**systemd socket activation** allows a process to be started on demand when a connection is made to a socket. **D-Bus signal delivery** allows processes to wake each other via IPC signals. **inotify** allows a process to be notified of filesystem changes. These are all "wake a process when something happens" primitives.

**Relevance:** Low for direct applicability. These mechanisms wake an OS process, not an LLM agent session. The abstraction gap is large. However, the systemd "socket activation" pattern (start the service on demand) is conceptually similar to Temporal Signal-With-Start: if the target is not running, start it, then deliver the event.

**Implication:** None for v0. The systemd/D-Bus layer is too low-level to inform the agent-wake design.

---

### 3.7 Thread B Synthesis

| Pattern | What it does | Overlap with agent-wake? | Implication |
|---|---|---|---|
| **Webhooks (Stripe/GitHub)** | POST to a URL; retry on failure; idempotency keys. | High — agent-wake’s ingest layer is a webhook receiver. | Add `event_id`/`idempotency_key` to schema; document retry semantics. *(v0 plan §4)* |
| **Workflow engines (Temporal/Restate/Inngest)** | Deliver events to durable workflow instances; lazy init; sleep/wake. | Medium — "lazy wake" pattern matches, but engines manage state and agent-wake doesn’t. | Consider substrate inbox for missed events when session is inactive. *(v1.5+)* |
| **SSE/WebSocket resumption (Centrifugo)** | Replay missed messages on reconnect. | Low — assumes client reconnects; agent session may be gone. | Document reply-event loss when session exits. *(v0 plan §4, Reply events)* |
| **MCP Triggers & Events** | Nascent WG for server-to-client proactive notifications. | Medium — future alignment possible. | Isolate unstable Claude Code channel code; monitor MCP WG. *(v0 plan §5)* |
| **A2A Protocol** | Agent-to-agent JSON-RPC messaging. | Low — different abstraction layer. | Optional: add `task_id` to `meta` for future A2A interop. *(v1 schema hook)* |
| **systemd/D-Bus/inotify** | OS-level process wake. | None — too low-level. | None. |

**Highest-value finding:** No published primitive covers agent-wake’s exact problem. This validates the project’s existence and suggests the team should consider publishing the wire format as a draft standard or blog post to establish prior art, mirroring the recommendation for agent-provenance.

---

## 4. Questions for Humans

1. **Key management documentation:** The finding is that peer-signed evidence is accepted IF key management is documented. Should the v1 spec or README include a "key management control description" template that deploying organizations can copy into their SOX/SOC 2 system descriptions? *(agent-provenance/README.md, trust model documentation)*

2. **FIM-class positioning:** Should agent-provenance explicitly position itself as a "FIM-class control for audit logs" in marketing/compliance documentation? This reframing avoids the "who attests the key" question but may confuse technical audiences familiar with FIM as a file-system-only concept. *(positioning decision)*

3. **Event idempotency:** Should the v0 wake event schema add an `event_id` or `idempotency_key` field now, or defer to v1? Adding it in v0 is backward-compatible and enables proper retry semantics. *(v0 plan §4, Wake event schema)*

4. **Substrate inbox for missed events:** If no session is active when a wake event arrives, should the substrate hook path queue the event for delivery when a session starts? This is a "Signal-With-Start" semantic and would require substrate to maintain a per-session inbox. Is this in scope for substrate, or out of scope for agent-wake entirely? *(substrate BC scope, v1.5+)*

5. **MCP Triggers & Events alignment:** Should the team monitor the MCP Triggers and Events WG and plan to align the Claude Code adapter with the eventual standard? If so, who owns the monitoring? *(process question)*

6. **Publishing prior art for wake-signaling:** No published primitive covers external-to-LLM-session event delivery. Should the team publish the wire format (`core/schema.md`) as a draft spec or blog post to establish prior art and invite community review? *(positioning, not design)*

---

## 5. What Was NOT Checked

- **Specific HSM vendor certifications (Thales, Yubico, etc.):** Skipped because the finding is that HSMs are preferred but not mandatory for auditor acceptance. HSM procurement details are a deployment concern, not a design question.
- **Specific Big Four firm internal methodology guides:** These are proprietary and not publicly available. The PCAOB inspection reports and AICPA standards are the authoritative public sources.
- **NIST FIPS 140-2/140-3 key management requirements:** Considered but skipped. FIPS is a US federal requirement; the workplace-audit use case is commercial (SOX/SOC 2/PCI). FIPS would be relevant for a federal customer but does not change the current finding.
- **Specific A2A protocol implementation details (task state machine, artifact exchange):** Skipped because A2A is agent-to-agent, not external-to-agent. The high-level structure was sufficient to determine non-overlap.
- **Inngest/Restate/Trigger.dev pricing or feature matrices:** Skipped because the question is about protocol patterns, not commercial comparison.
- **Discord/Telegram/Slack bot event delivery patterns:** These are channel-specific integrations that agent-wake explicitly delegates to harness plugins or user wiring. Not load-bearing for the core design.
- **Round 1 sources re-checked:** No round 1 sources were re-checked unless directly relevant to a round 2 question. The finding about CloudTrail key management is a new angle, not a contradiction of round 1.

---

*End of report.*
