# Plan 002 — Secret management: URI-addressed secrets, Vault backend, lifecycle CLI

**Status:** Draft, ready to implement.
**Scope:** daemon only. Adapters don't touch HMAC secrets — they're a daemon concern.
**Motivation:** Bootstrapping today requires the operator to run `generate-secret.py`, export the right env var name, and edit `config.json` by hand. That's the friction the operator just hit. Two shifts make this much better:

1. Reference secrets by **URI** in config (`env://NAME`, `vault://path#key`) instead of hard-coding `secret_env`. Lets each source pick its own backend.
2. Ship `agent-wake secrets {add,rotate,list,remove}` as the single owner of the secret lifecycle so operators stop hand-editing config and env files.

The legacy `secret_env` field stays working — no breaking change for the existing config the operator already has on disk.

---

## Design decisions (locked from conversation)

1. **Vault auth:** AppRole. `role_id` lives in config, `secret_id` lives in a mode-600 file. `systemd-creds` is the documented hardening step but not v1's default.
2. **Cache TTL:** 15 min for KV-static, lease⅔ for dynamic-with-lease, `SIGHUP` forces immediate refresh across all sources. Per-source override via `?ttl=5m` URI query. Matches Vault Agent conventions.
3. **CLI is backend-aware:** `agent-wake secrets add --backend env|vault [opts]`. Each backend owns its own add/rotate/remove semantics.
4. **Print-once on add.** No `secrets export` command. Vault-mode can legitimately re-read from Vault; env-mode forces the operator to record the secret at creation.
5. **Rotation requires zero-downtime support.** A source may carry a *list* of valid secrets; HMAC verification accepts if any match. `rotate` prepends the new and keeps the previous until the next rotate.

## Touch surface

Single-secret consumption point is `daemon/src/agent_waked/ingest.py:119`:

```python
if not verify_signature(raw_body, source_cfg["secret"], signature):
```

That becomes "any-of secrets verifies." Outside `config.py` and `ingest.py`, no other daemon module touches secrets, so the blast radius is small.

New surfaces:
- `daemon/src/agent_waked/secrets/` — backend abstraction (`base.py`, `env.py`, `vault.py`)
- `daemon/src/agent_waked/cli/` — `secrets` subcommand (new entry point `agent-wake`)
- `daemon/pyproject.toml` — add `hvac` as an *optional* extra (`pip install agent-waked[vault]`); core install stays dependency-light

## Config schema (v1, additive)

```jsonc
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "sources": {
    // Legacy form — still accepted, unchanged
    "demo": { "secret_env": "AGENT_WAKE_DEMO_SECRET" },

    // New form — single secret by URI
    "github-actions": { "secret": "env://AGENT_WAKE_GITHUB_SECRET" },

    // New form — multiple secrets (rotation window)
    "regista": {
      "secrets": [
        "vault://secret/data/agent-wake/regista#current",
        "vault://secret/data/agent-wake/regista#previous"
      ]
    }
  },

  // New top-level section, only required when any vault:// URI is used
  "vault": {
    "addr": "https://vault.internal:8200",
    "namespace": "engineering",          // optional
    "auth": {
      "method": "approle",
      "role_id": "...",
      "secret_id_file": "~/.config/agent-wake/vault-secret-id"
    }
  }
}
```

**Validation rules:**
- `secret_env`, `secret`, and `secrets` are mutually exclusive per source. Exactly one must be present.
- `secrets` must be a non-empty list of strings. Order matters: index 0 is "current" (the secret used by `rotate`'s "keep one previous" logic).
- A `vault://` URI in any source requires the top-level `vault:` block.

## URI grammar

```
env://NAME[?ttl=...]                 # NAME = env var; ttl ignored (env is static)
vault://PATH#KEY[?ttl=15m]           # PATH = KV path, KEY = field inside the secret
```

`ttl` is parsed as a Go-style duration (`30s`, `5m`, `1h`). Defaults: env → infinity, vault-static → 15m, vault-dynamic → lease⅔ (overrides `ttl`).

## Resolver abstraction

```python
# secrets/base.py
class SecretBackend(Protocol):
    async def resolve(self, uri: str) -> bytes: ...
    async def refresh(self, uri: str) -> bytes: ...   # bypass cache
    def supports(self, scheme: str) -> bool: ...

# secrets/resolver.py
class SecretResolver:
    def __init__(self, backends: list[SecretBackend], vault_cfg: dict | None): ...
    async def resolve(self, uri: str) -> bytes:           # cache-aware
    async def resolve_all(self, source_cfg: dict) -> list[bytes]:  # for HMAC any-of
    async def refresh_all(self) -> None:                  # SIGHUP handler
```

Cache key = URI string. Cache entry = `(value: bytes, expires_at: float)`. Refresh on miss, on expiry, on SIGHUP, on explicit `refresh()`.

## Ingest change

`gating.verify_signature` already takes a single `secret: bytes`. Add `verify_signature_any(body, secrets: list[bytes], header) -> bool` that short-circuits on first match. Update the one call site in `ingest.py`. Old single-secret helper stays for tests and callers that still use it.

## `agent-wake secrets` CLI surface

New console script `agent-wake` in `daemon/pyproject.toml` (so it ships with `pip install -e daemon/`). Subcommands:

```
agent-wake secrets add <source> --backend env [--name VAR]
agent-wake secrets add <source> --backend vault --path PATH [--key value]
agent-wake secrets rotate <source>                      # backend inferred from current config
agent-wake secrets list                                 # source → backend, no values
agent-wake secrets remove <source>
```

Behavior:
- `add` generates a fresh 32-byte secret, writes it to the chosen backend, updates `config.json` to point at it (creating `sources.<name>` if new), prints the secret value **once** to stdout with a "store this for the sender" header.
- `add` for `--backend env`: writes to `~/.config/agent-wake/secrets.env` (chmod 600 enforced), appends a line `<VAR>=<value>`, never overwrites existing entries — if `<source>` already exists, refuse and tell the operator to `rotate`.
- `add` for `--backend vault`: writes to Vault using the configured auth, then updates `config.json`'s `secret` field.
- `rotate`: generates new secret, *prepends* to `secrets:[…]` list (auto-promoting `secret:` → `secrets:[]` on first rotation), trims to 2 entries (current + previous), prints once, sends `SIGHUP` to the running daemon if it's up.
- `list`: tabular output, one line per source, columns = `source | backend | n_secrets | rotated_at?`. No secret material printed, ever.
- `remove`: drops the source from `config.json` and removes the env-file line / Vault path (with confirmation for vault).
- `list` and `remove` are backend-agnostic. `add` and `rotate` are backend-aware.

Errors and refusals:
- Any command that mutates `config.json` writes to a temp file in the same dir and atomically renames. Validates the new config parses cleanly before commit; aborts on parse failure.
- Refuses to operate if `config.json` doesn't exist — direct the operator to `agent-wake init` (future, not this plan).

## SIGHUP integration

The daemon already supports SIGHUP for config reload. Extend to also call `resolver.refresh_all()` so vault-mode picks up rotations without restart. The CLI's `rotate` sends SIGHUP after writing the new secret. No-op if the daemon isn't running.

---

## Implementation order

### Step 1 — Config schema + parser

- `config.py`: add URI parsing, `secret`/`secrets`/`secret_env` exclusivity, vault-block validation.
- Output shape changes: `cfg["sources"][name]` becomes `{ "secret_uris": [...], "callback_url": ... }`. Drop the eager-resolved `"secret": bytes` field — resolution moves to the resolver, called from ingest.
- Update `config.py` unit tests: legacy `secret_env` still works; new `secret`/`secrets` shapes validated; mutex enforced; vault block required when used.

### Step 2 — Resolver + env backend

- `secrets/base.py`, `secrets/env.py`, `secrets/resolver.py`.
- Env backend: read env var at first `resolve`, cache forever (TTL=∞). `refresh` re-reads — env vars *can* change across reloads if systemd's EnvironmentFile was edited and the unit restarted, but in practice env is static; document this.
- Tests: cache hit/miss, refresh, missing env var error path.

### Step 3 — Wire resolver into ingest

- `ingest.py`: `await resolver.resolve_all(source_cfg)` → list of bytes → `verify_signature_any`.
- `gating.py`: add `verify_signature_any`.
- `main.py`: instantiate the resolver at startup, pass into ingest handler, register SIGHUP hook for `refresh_all`.
- Tests: integration test asserting any-of HMAC behavior with two valid secrets and one invalid signature.

### Step 4 — Vault backend

- `secrets/vault.py` using `hvac`. AppRole login at startup; re-login on token expiry. KV v2 read for `vault://secret/data/...` paths. TTL = 15m default or `?ttl=` override or lease⅔ if leased.
- `pyproject.toml`: `[project.optional-dependencies] vault = ["hvac>=2.0"]`. Resolver raises a clear "install agent-waked[vault]" error if a `vault://` URI is configured but `hvac` is missing.
- Tests: mock `hvac.Client`, assert read path, TTL behavior, SIGHUP-triggered refresh, AppRole login retry.

### Step 5 — `agent-wake` CLI

- New module `cli/__init__.py`, `cli/secrets.py`.
- New console script in `pyproject.toml`: `agent-wake = "agent_waked.cli:main"`.
- Implement subcommands in order: `list`, `add --backend env`, `rotate` (env), `remove`, `add --backend vault`, `rotate` (vault).
- Tests: each subcommand against a temp HOME with a fixture config.

### Step 6 — Docs

- Update `daemon/README.md`:
  - Replace the "generate secret + export env var + edit config" walkthrough with `agent-wake secrets add demo --backend env`.
  - New "Vault backend" section: example config, AppRole setup, `agent-wake secrets add <s> --backend vault`.
  - "Rotating a secret" section.
- Update top-level `README.md` quick-start to use the CLI.
- New `daemon/docs/secret-management.md` for the deep dive (backends, TTL semantics, rotation flow, SIGHUP behavior).

### Step 7 — Operator's existing config

The operator already has `~/.config/agent-wake/config.json` using the legacy `secret_env` form. It must continue to work after upgrade. Step 1's parser tests cover this; verify on the operator's actual config in step 6.

## Out of scope (future plans)

- `agent-wake init` for greenfield bootstrap.
- Non-AppRole Vault auth (K8s, AWS IAM, OIDC).
- `systemd-creds` integration for the AppRole `secret_id`.
- Webhook-registration helpers (`agent-wake secrets export-for-sender`).
- Secret backends beyond env and Vault (1Password, Bitwarden, AWS Secrets Manager).

## Verification

- [ ] `cd daemon && pytest -q` — all green
- [ ] `mypy src/` — clean
- [ ] Manual: existing operator config (`demo-claude`, `demo-opencode` with `secret_env`) loads unchanged, daemon serves a signed POST successfully.
- [ ] Manual: `agent-wake secrets add testsource --backend env` creates source, prints secret once, daemon SIGHUP picks it up, signed POST against new source succeeds.
- [ ] Manual: `agent-wake secrets rotate testsource` promotes to two-secret list, both old and new secrets verify, next `rotate` evicts the original.
- [ ] Manual (if Vault available locally): `agent-wake secrets add vsource --backend vault --path secret/agent-wake/vsource` writes, resolves, verifies.
