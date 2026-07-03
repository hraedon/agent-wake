# Plan 004 — Suite cohesion: external signaling as a suite component

**Status:** Proposed 2026-07-02
**Author:** Claude (Fable 5), from the 2026-07-02 agent-suite deployment review
**Strategic role:** agent-wake is the suite's external→session signaling: it lets
CI, monitors, and cross-session messages drive agent work instead of only the
terminal. In the suite it is Tier 2 (blueprint §1) — genuinely optional for a
first deployment, but the thing that turns "an agent you sit in front of" into
"an agent the estate's events can reach." This plan makes wake a clean suite
component: its secret management landed, its adapters installable as a suite step,
and its config/doctor conforming. See `/projects/agent-suite-blueprint.md`
(Phase D, Tier 2).

## Ground truth at time of writing

- agent-wake is on `hraedon/agent-wake`; `main` is 1 ahead of origin with an
  uncommitted working tree (claude + opencode adapter edits, identity-layer work).
  Plan 001 (substrate→regista rename) is closed; an identity layer (per-source
  `principal_id`, trigger allowlist, schema hooks) landed.
- **Two plans are open and directly relevant:** Plan 002 (secret management) and
  Plan 003 (v0.1 dogfood + reliability). Secret management is the gating cohesion
  item — a signaling daemon that external systems POST to needs a credible auth/
  secret story before it's deployable at work.
- wake has claude and opencode **adapters** (the harness-side receivers) plus a
  daemon; it has no container/deploy artifacts yet.
- Config env not yet converged on the suite vocabulary; wake predates the
  contract.

## Principles this plan must hold

- **Optional by design, clean when present.** The suite must be fully functional
  without wake; when wake is deployed, it conforms to the same config/doctor/
  install idioms as the rest — no special-casing.
- **Signaling authenticates.** An external-event ingress is an attack surface;
  the secret-management work (Plan 002) is a precondition of deployment, not a
  nice-to-have. Off-by-default, explicit allowlist, authenticated triggers.
- **Adopt the shared facts.** Where wake touches regista (identity/schema hooks),
  read `REGISTA_DSN`/`REGISTA_KEY_PATH` (regista Plan 025) with a one-release
  alias; wake-specific config stays wake's.

---

## Phase 1 — Land the gating work

### WI-1.1 — Secret management (execute Plan 002)
- Bring Plan 002 to shipped: authenticated trigger ingress, secret/token custody
  reading from the suite secret store, the per-source `principal_id` allowlist
  enforced on every trigger. Commit the in-flight adapter/identity work first
  (working tree is dirty and `main` is ahead of origin — reconcile before
  building on it).
- **AC:** an unauthenticated trigger is rejected; an allowlisted, authenticated
  trigger delivers; secrets come from the suite store, never a committed file;
  the working tree is clean and pushed.

## Phase 2 — Suite config + doctor

### WI-2.1 — Canonical `REGISTA_*` + `wake doctor --json`
- Read regista DSN/key via the suite precedence where wake uses regista; conform
  `doctor` to the suite shape: `{component:"agent-wake", version, regista:{…} if
  used, checks:[ingress reachable, auth configured, adapters installed,
  allowlist present, …]}`.
- **AC:** operates reading `suite.env`; `doctor --json` validates against the
  suite shape; an unconfigured-auth state is a named check failure (so wake can't
  be deployed accidentally open).

## Phase 3 — Installable adapters + packaging

### WI-3.1 — Adapter install as a suite step + daemon packaging
- Fold the claude/opencode adapter install into the shared `install-harness`
  idiom (agent-notes 017 / cairn 008 / acb 005), and package the daemon as a
  deployable artifact (container or systemd unit per the operator's substrate
  decision, blueprint §3.1).
- **AC:** `install-harness` wires wake's receiver into a named harness
  idempotently; the daemon runs from the packaged artifact reading only
  `suite.env` + secrets; a fresh machine can add wake without hand-editing.

## Phase 4 — Cross-platform, secrets, publication

### WI-4.1 — Secrets from the backend; Windows Service + Linux daemon
- The trigger-ingress auth secret (WI-1.1) and any regista credentials resolve via
  `regista.secrets.resolve` (Plan 025 WI-1.2) — Vault/AKV/Windows, no plaintext.
  Package the daemon as a **Windows Service** as well as a Linux systemd unit /
  container (blueprint substrate: Linux/Docker/Windows, no k8s).
- **AC:** the ingress secret resolves from each backend (gated tests); the daemon
  runs as a Windows Service surviving reboot and as a Linux unit; no plaintext
  secret on disk.

### WI-4.2 — Publication gate (sanitize before flipping public)
- Before agent-wake flips public (blueprint §3): reconcile the dirty tree /
  ahead-of-origin state first, then filter-repo scrub, CI identifier-gate, and a
  publication-review checklist.
- **AC:** working tree clean and pushed; history clean of work-domain identifiers
  (verified); identifier gate green; checklist complete before the flip.

## Sequencing & notes

- **Harness note (2026-07-02, revised):** work deployment is Claude-first, but the
  operator runs **both harnesses locally**, so the **opencode adapter is kept at
  parity, not deferred** — WI-3.1 wires both receivers and a dual-harness validation
  confirms the cohesion changes (config vocabulary, secret resolution) don't regress
  an existing opencode setup. Cheap, since both are testable locally.
- **Tier 2 — sequence this after the Tier 0–1 core (blueprint Phases A–C) is
  proven.** wake earns its deployment once there's agent work worth signaling into;
  building its suite surface before the faces are up is premature.
- **WI-1.1 (secret management) gates everything here** and also gates any work
  deployment — an open ingress is a non-starter in a regulated environment.
- The reliability/dogfood work (Plan 003) is a wake product concern, parallel to
  this cohesion plan; do not couple them.
- First reconcile the dirty tree / ahead-of-origin state — don't build cohesion
  work on an unpushed base (the same gotcha that bit the family before).
