# Plan 005 — Human-directed delivery: wake the human, not just the agent

**Status:** In Progress — human delivery implemented; operational loop remains.
**Author:** Claude (Fable 5), from the 2026-07-07 suite v2 gaps review
**Strategic role:** agent-wake today moves signals *into* agent sessions.
The suite's v2 human-visibility story needs the mirror image: signals that
reach a *human* — "an item awaits your accept" (the strict-gate bottleneck),
"suite doctor went red," "chain verify failed." dossier Plan 018 defines what
gets emitted; this plan makes agent-wake the delivery leg, keeping the suite's
signaling in one component instead of teaching dossier SMTP.

## Ground truth at time of writing

- agent-wake's daemon + adapters (claude, opencode) are live on the operator
  box; ingress is authenticated per Plan 004 WI-1.1. Doctor reports one warn:
  no sources have `principal_id` configured (identity layer inactive).
- There is no outbound path to a human anywhere in the suite. Gate-blocking
  events stall silently until someone happens to open dossier.
- dossier Plan 018 WI-2.1 (proposed same day) emits notification events to a
  webhook sink; agent-wake's ingress is the intended first-class target.

## Principles

- **Delivery backends are adapters, exactly like harnesses.** A human channel
  (webhook, email) is one more adapter with its own secret custody per Plan 004
  discipline — no bespoke side channel.
- **At-least-once with idempotency keys.** A missed accept-nudge silently
  stalls a pipeline; a duplicate is a minor annoyance. Prefer redelivery.
- **Identity first.** Routing "to the reviewing human" requires the
  principal_id layer that doctor already warns about — finish it rather than
  routing by ad-hoc addresses.

---

### WI-1.1 — Finish the principal identity layer
- Close the existing doctor warn: sources carry `principal_id`; the routing
  table maps principal → delivery channel(s). This is the same principal
  vocabulary as regista (Plan 026 enrollment), not a parallel identity.
- **AC:** doctor's `allowlist_present` warn clears; a delivery routed to an
  unknown principal is rejected loudly, not dropped.

### WI-1.2 — Outbound webhook adapter (first channel)
- A generic signed-webhook deliverer: dossier (or any suite component) posts a
  notification event to wake's ingress; wake delivers it to the principal's
  configured webhook (chat systems, home-automation, test receivers) with
  retry + idempotency key. Secret custody via the Plan 004 backend contract.
- **AC:** dossier Plan 018's `awaiting_your_accept` event, posted to ingress,
  arrives at a test receiver with the deep link intact; a dead receiver retries
  and surfaces as a doctor warn, not silence.

### WI-1.3 — Email adapter (second channel)
- SMTP delivery for shops where webhooks aren't the idiom (the regulated
  deployment likely wants mail). Templates for the Plan 018 event classes +
  digest payloads. TLS + auth via the secret backend.
- **AC:** the digest event renders as a readable email; SMTP failure is a
  doctor finding.

### WI-1.4 — Operational alerting loop
- The suite's own health uses the same path: a scheduled `agent-suite doctor`
  run (agent-suite Plan 005 owns the scheduling) posts red/degraded results to
  ingress; wake delivers to the operator principal. Same for
  `chain_verify_failed` from the verifier.
- **AC:** forcing a component red on a box with the loop configured produces a
  delivered alert within the schedule interval; recovery delivers an all-clear
  (or is visibly suppressed by config).

---

## Sequencing

WI-1.1 first (identity underpins routing). WI-1.2 pairs with dossier Plan 018
WI-2.1 for the live path; WI-1.3/1.4 follow. Tier-2 caveat stands: none of
this blocks the Tier 0–1 core, but the strict-gate deployment is materially
less usable without WI-1.2, so schedule it with the human-visibility wave.

## Implementation note — 2026-07-11

WI-1.1 through WI-1.3 are implemented. The dossier integration now has an
executable contract proof covering HMAC authentication, service-identity gating,
explicit target authorization, human dispatch, and denial of an unapproved
principal. Email rendering recognizes dossier's canonical
`awaiting_your_accept`, `review_requested`, and `item_returned` event kinds.
WI-1.4 remains dependent on the suite-owned scheduled operational alert loop, so
the plan remains In Progress.
