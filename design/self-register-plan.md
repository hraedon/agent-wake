# Self-registration: per-session labeled routing

**Status:** plan, not yet implemented. Concrete enough that an
implementing agent can land each tier without re-deriving the design.

**Problem.** Today the opencode adapter broadcasts every wake event to
all live opencode sessions (or to a single session named in
`event.meta.session_id`). With five sessions open, "send this to the
oncall session" is not expressible. The Claude Code adapter has the
same gap when more than one `claude` process is running on the same
host — they share the daemon, but the daemon has no concept of which
process should receive which event.

**Goal.** Let an agent inside a session announce *I am `oncall`* (or
`build-bot`, `customer-23`, …) and have external systems POST events
that target only sessions/processes carrying that label.

The two failure modes — multiple sessions in one process (opencode),
multiple processes on one host (CC) — need different mechanisms.
This plan has a tier for each.

---

## Tier 1 — Plugin-side per-session subscribe (opencode)

**Where it lives.** Entirely inside `adapters/opencode/`. Daemon
unchanged. Event wire format unchanged except for a new convention on
`meta.target`.

**New tools registered by the plugin:**

```
agent_wake_subscribe(label: string)
  → "subscribed session <sid> to <label>"

agent_wake_unsubscribe(label?: string)
  → "unsubscribed session <sid> from <label>"      # label given
  → "unsubscribed session <sid> from all labels"   # label omitted

agent_wake_status()
  → "session <sid> labels: [oncall, build-bot]"
```

Tool `execute(args, context)` receives `context.sessionID`. That is
the self-attestation: the agent calling the tool can only register the
session it is running in.

**State.** Module-scoped `Map<sessionID, Set<label>>` in `src/labels.ts`.
v1 is in-memory only. On opencode restart, agents re-register on next
turn. Persistence (a JSON file under `~/.local/state/agent-wake/`) is
a follow-up; flagging it in `agent_wake_status` output ("labels are
not persisted across opencode restarts") is enough to keep
expectations calibrated.

**Wake handler change** (`src/wake.ts`):

```ts
const target = event.meta?.target;
if (typeof target === "string" && target.length > 0) {
  const targets = sessionsWithLabel(target);
  if (targets.length === 0) {
    log.warn(`no session subscribed to label '${target}', dropping`);
    return { delivered: 0, sessions: [] };
  }
  // ... promptAsync per target
} else if (typeof event.meta?.session_id === "string") {
  // existing direct-id path, unchanged
} else {
  // existing broadcast path, unchanged
}
```

Targeted delivery wins over `session_id` only if `meta.target` is set.
If both are set, prefer `target` and log a warning.

**Garbage collection.** Hook the opencode `event` hook for
`session.deleted` (already in the SDK event stream) and drop labels
for that session. Without this, stale entries accumulate; not unsafe,
just messy.

**Schema doc.** `core/schema.md` adds a paragraph: `meta.target` is
the canonical routing key; the daemon does not interpret it; adapters
MAY use it for sub-adapter routing.

**Tests** (in `adapters/opencode/tests/labels.test.ts`):
- subscribe stores label; status reflects it
- unsubscribe removes one label; unsubscribe(no arg) removes all
- wake with `meta.target` delivers only to subscribed sessions
- wake with `meta.target` and no subscribers → dropped with warn
- session.deleted event triggers GC
- two sessions can hold the same label (fan-out)

**Acceptance.** From a running opencode session, agent runs
`agent_wake_subscribe("oncall")`. External `curl` with
`meta.target: "oncall"` arrives in that session and no others. With
five sessions, only those that subscribed see it. Existing
broadcast-when-no-target behaviour preserved.

**Estimated size.** ~120 lines of TS in adapters/opencode/, ~80 lines
of tests, a paragraph in core/schema.md. One PR.

---

## Tier 2 — Daemon-side labeled subscribers (multi-process)

**When you need it.** Five Claude Code processes on one box. Two
opencode-server instances on one box. Any case where the wake target
spans process boundaries, not just sessions inside one process.

**`hello` frame extension** (v1-daemon-spec.md §4.3.1):

```json
{
  "type": "hello", "v": 1,
  "adapter": "claude", "instance": "pid-12345",
  "filters": {"sources": ["github-actions"]},
  "labels": ["oncall", "team-a"]      // NEW, optional
}
```

Unknown to v1 daemons: ignored (`labels` is additive, no version bump
needed; documented as forward-compat).

**Router change** (`daemon/src/agent_waked/router.py`):
- Maintain `label_index: dict[str, list[Subscription]]`.
- On `subscribe`: index each label.
- On `unsubscribe`: drop from index.
- `deliver(event)`:
  - if `event.meta.target` is set and a `labels` index exists: route
    only to subscriptions whose `labels` contain that target AND whose
    `sources` accept the event's source. If none match: `no_subscriber`.
  - else: existing source-based routing.

Compatibility: a subscriber that sends no `labels` matches no targeted
event. That's intentional — opt-in. Untargeted events still reach
unlabeled subscribers as today.

**Adapter side (CC):**

```python
# adapters/claude/src/agent_wake_claude/config.py
labels_env = os.environ.get("AGENT_WAKE_INSTANCE_LABEL")
config["labels"] = labels_env.split(",") if labels_env else []
```

```python
# client.py hello
hello = {..., "labels": cfg.get("labels", [])}
```

User invocation becomes:

```bash
AGENT_WAKE_INSTANCE_LABEL=oncall \
  claude --dangerously-load-development-channels server:agent-wake-claude
```

**Adapter side (opencode):** the plugin's hello also carries the
host-level label (read from env or config). Plugin-level
per-session labels (tier 1) compose on top — the daemon picks the
right *process*, the plugin picks the right *session* inside it.

**External callers** send `meta.target: "oncall"` in the v0 event body.
The HMAC signature already covers the body; no additional auth
surface introduced by this change. A header form
(`X-AgentWake-Target`) is not added — keeps the wire surface small
and avoids the routing decision diverging between body and header.

**Tests** (`daemon/tests/test_router.py`, `test_e2e.py`):
- subscriber with labels=[a,b] receives target=a, target=b, no target
- subscriber with labels=[a] does NOT receive target=b
- two subscribers with labels=[a]: deliver fan-out (or pick most-recent
  per existing policy — confirm and document)
- targeted event with no matching label → `no_subscriber`, 202 with
  `{"status": "no_subscriber"}` (mirrors source-no-subscriber today)

**Acceptance.** Two `claude` processes on one host, one launched with
`AGENT_WAKE_INSTANCE_LABEL=oncall`, one with `AGENT_WAKE_INSTANCE_LABEL=build`.
External `curl` with `meta.target: "oncall"` arrives in the first
process only. The build process sees nothing. Targeted event with
unknown label is dropped cleanly.

**Estimated size.** ~80 lines daemon + ~20 lines CC adapter + ~20
opencode adapter + ~100 lines daemon tests + schema doc paragraph.
One PR.

**Sequence with tier 1:** tier 2 is independent of tier 1; either can
land first. Recommend tier 1 first because (a) it unblocks the
five-opencode-sessions story you actually have today and (b) it
forces the `meta.target` convention to be exercised before the daemon
side commits to it.

---

## Auth: the squatting problem

Both tiers above have the same gap: **label namespace is
unauthenticated**. In tier 1, any opencode session can claim any
label. In tier 2, any process that can connect to the daemon's unix
socket can claim any label. There is no notion of *who* the claimant
is, so there is no way to say *this label belongs to that party*.

In single-user / single-machine deployments (the v1 target), the
threat model is **misconfiguration, not adversary**. A
self-inflicted collision — two sessions both claim `oncall` and
fight over events — is annoying but not security. Filesystem mode
`0600` on the unix socket already excludes other Unix users. Outside
the socket, HMAC gates ingress at the daemon. So at v1, the practical
risk is low.

The real exposure shows up later:

- **Multi-user shared host** (one daemon, several users, one
  filesystem). `0600` doesn't help — every user can connect as the
  daemon owner if filesystem ACLs are misconfigured. Label squatting
  becomes a denial-of-routing vector (steal someone's events, or
  flood theirs).
- **Cross-org delegation** (an agent acting on behalf of someone
  else). The `on_behalf_of` chain says *who* is acting; without
  scoped labels, that identity has no expression in the routing
  layer.

### My view on how to address it

**Do nothing in tier 1 / tier 2.** Ship the un-authed version. Call
out the limitation in the README ("any session can claim any label;
single-user use only") and in `core/schema.md`. Forcing auth into
the routing layer now means either inventing a parallel identity
system or blocking on BC-196 — neither is a good use of the next PR.

**Defer auth to identity v1** (`design/v1-implementation-spec.md`).
The identity spec already proposes:

- `principal_id` carried in the signing envelope (BC-214/216)
- `session_grant` events binding an agent's ephemeral key to a human
  identity for a bounded scope and lifetime (BC-219)
- Per-user sender allowlists for cross-user consent

Labels compose with that cleanly: the convention becomes
`label = "<principal_id>:<role>"`, and the daemon validates that
the connecting adapter's authenticated `principal_id` matches the
prefix on every label it claims. Squatting is then impossible by
construction — you can only claim labels that start with your own
identity. Cross-principal labels (e.g. `team-shared`) need an
explicit grant event, same shape as `session_grant`.

This is **not** something to design before BC-196 lands. The
identity spec round 3 already considered this surface area (auditor
keys, role gates, session grants); routing labels are one more
instance of the same problem and should reuse that mechanism, not
invent a parallel one.

**Three intermediate options** that exist if waiting for identity v1
turns out to be too long:

1. **Config allowlist.** `routing` block in daemon config declares
   legal labels and which `instance` strings (or HMAC-secret-derived
   identifiers) may claim each. Static, cheap, no crypto. Works for
   the regulated-workplace use case where ops controls config.
2. **Daemon-issued tokens.** Adapter calls `register_label` over the
   socket, daemon returns a one-shot token, adapter presents it on
   subsequent hello. Solves "drive-by claim" but not "first-claim-
   wins" races. Adds a round trip and a token store.
3. **First-come leases with expiry.** Labels held for N minutes,
   refresh required. Doesn't actually solve auth, just makes
   squatting transient. Useful for cleanup, not security.

Of those, option 1 is the only one I'd ship if forced to do
something before identity v1 — its complexity is in *config* not in
*code* (much cheaper to maintain), and it matches how the existing
HMAC-secret-per-source model already works. Options 2 and 3 add code
weight for limited benefit; skip them.

### Recommended path

1. Tier 1 (this week). Ship per-session subscribe in the opencode
   adapter. Note the unauthenticated nature in code comments and
   README.
2. Tier 2 (when needed). Ship daemon-side labeled subscribers when
   you actually have multi-process use. Same caveat.
3. Identity v1 (separate work item, paced by substrate BC-196).
   When that lands, retrofit labels: enforce
   `label.startsWith(principal_id + ":")` at registration time.
   Stale labels claimed by un-authenticated subscribers expire on
   the first reconnect.
4. **Do not** invent a parallel token system. The amount of code
   it would replace in step 3 is small; the amount of code it would
   need to maintain in the meantime is not.

---

## What this doesn't cover

- **Fan-out semantics.** Two subscribers both claim `oncall` — do we
  deliver to both, or pick most-recent? Tier 2 spec above defers this
  to the existing daemon policy (most-recent), but fan-out may be
  what you want. Decide at implementation time; either is a one-line
  change.
- **Discovery.** External callers learn label names out-of-band (you
  tell them, or a config file shipped with the integration). No
  daemon endpoint exposes a label list. Adding one trivially leaks
  the label namespace to anyone who can hit `/`; better to keep it
  opaque.
- **Heartbeats / liveness.** A subscriber claims a label, then its
  process dies without unsubscribing. Today the daemon clears the
  subscription on socket close, so the label drops naturally. If we
  add durable label storage (Tier 1 persistence), garbage collection
  becomes a real question — flag it then, not now.

---

## Estimated total work

- Tier 1 (opencode plugin): ~one PR, half a day for an implementer
  familiar with the codebase.
- Tier 2 (daemon + CC + opencode hello): ~one PR, one day.
- Identity-v1 retrofit: ~half a day once BC-196 / BC-216 are in,
  largely test changes plus an `assert label.startswith(...)` line.

All three are small enough that each can be a single focused session.
