# Plan 001 — Consumer migration: substrate → regista

**Status:** Phase 2 of the cross-project rename. **Blocked until substrate Plan 018 completes** and `v0.4.0` is tagged.
**Scope:** agent-wake specifically. See `/projects/RENAME-substrate-to-regista.md` for orchestration context.
**Substrate refs in this repo:** 31.

This is agent-wake's first plan; the `plans/` directory was created for this work. Future plans can extend the numbering.

---

## Pre-flight

- [ ] Substrate has tagged `v0.4.0` with the rename complete.
- [ ] Daemon and adapter tests pass on current main:
  - `cd daemon && pytest -q`
  - `cd adapters/claude && pytest -q`
  - `cd adapters/opencode && bun test`
- [ ] Fresh branch: `git checkout -b rename/substrate-to-regista`.

## Where substrate appears in agent-wake

Pre-survey (31 refs) identified these touch surfaces:

- `AGENTS.md` — research-findings section explicitly names substrate as the "preferred durable-ingest path"
- `design/*.md` — multiple design docs reference substrate as the integration target, especially the identity / multi-user docs and `v0-implementation-plan.md`
- `design/identity-and-multi-user-v1-implementation-spec.md` — heavy substrate coupling (workflow events, signing envelope alignment)
- `daemon/README.md` — may reference substrate in deployment context
- Adapter READMEs (claude, opencode) — substrate composition notes

No runtime import dependency on substrate that I observed. Verify via `grep -rn 'import substrate\|from substrate' adapters/ daemon/ tools/`. If hits exist, those become `import regista` / `from regista`.

## Steps

### 1. Inventory

```bash
grep -rln '\bsubstrate\b\|\bSUBSTRATE\b\|\bSubstrate\b' \
  --include='*.py' --include='*.ts' --include='*.md' --include='*.toml' --include='*.yaml' --include='*.json' \
  . \
  | grep -v -E 'reflections/|node_modules/|\.venv/|\.git/|dist/|design/reviews/'
```

Note: `design/reviews/` contains historical model-review artifacts — treat as historical, don't edit.

### 2. Decide on design-doc treatment

The `design/` directory holds multiple rounds of model-review docs about identity / multi-user. Those docs explicitly debate "how substrate should handle X." Three buckets to triage:

- **Active spec docs** (`design/v1-daemon-spec.md`, `design/v1-implementation-spec.md`, `design/identity-position-taken.md`) — edit. These describe current design intent.
- **Review-round artifacts** (`design/research-findings-*.md`, `design/round-2-*.md`, `design/round-3-*.md`, `design/review-*.md`) — historical. The reviewers were reviewing "substrate." Leave as-is.
- **Open-question / debate docs** (`design/positions-on-identity.md`, `design/identity-and-multi-user.md`) — edit if marked active; leave if historical.

Make a decision per file before running sed.

### 3. Sed pass over live files

After triage, sed over the live list:

```bash
sed -i \
  -e 's/\bsubstrate\b/regista/g' \
  -e 's/\bSUBSTRATE\b/REGISTA/g' \
  -e 's/\bSubstrate\b/Regista/g' \
  <list-of-live-files-from-triage>
```

If any `pyproject.toml` (daemon, claude adapter) declares a `substrate` dependency, change to `regista` pinned `>=0.4.0`.

### 4. Code-path imports

```bash
grep -rn 'import substrate\|from substrate' \
  adapters/ daemon/ tools/ \
  | grep -v -E 'node_modules/|\.venv/'
```

For each hit, change `substrate` to `regista`. Likely zero or near-zero; agent-wake is designed to be substrate-optional.

### 5. Hand-review

Read `AGENTS.md` and the active design docs after sed. Specifically:

- `AGENTS.md` "What's been decided" section: ensure language like "Substrate is the preferred durable-ingest path" now reads "Regista is the preferred durable-ingest path" — and that the sentence still parses well. (It does.)
- `design/v1-daemon-spec.md` and `design/identity-position-taken.md`: scan for places where "substrate" was used as a *concept* not the *name of the project*. None expected, but check.

### 6. Tests

```bash
cd daemon && .venv/bin/pytest -q          # 93 tests
cd ../adapters/claude && .venv/bin/pytest -q    # 25 tests
cd ../opencode && bun test                # 50 tests
```

168 total; all should pass. If any test asserts a string containing "substrate" in error messages or log output, fix the expected value.

### 7. Commit

```bash
git add -A
git commit -m "rename: substrate → regista (Plan 001)"
git push -u origin rename/substrate-to-regista
```

PR, self-review, merge.

## Exit criteria

- [ ] Inventory grep returns 0 hits across live files.
- [ ] All three test suites green.
- [ ] PR merged.

## Intentionally not touched

- `reflections/*.md` — historical
- `design/reviews/*` — model-review artifacts
- `design/research-findings-*.md`, `design/round-2-*.md`, `design/round-3-*.md` — historical debate
- `design/identity-review-*.md` — historical model reviews
- `node_modules/` — vendored deps
- `dist/` — build artifacts

## Notes specific to agent-wake

The agent-wake design intentionally avoided importing substrate at runtime (substrate is the "preferred durable-ingest path" via HTTP bridge, not via library coupling). This means the rename is mostly a documentation/positioning change rather than a code change. If you find runtime imports of `substrate`, that's a discovery worth noting — the design said the runtime coupling shouldn't exist.

## Rollback

`git revert <commit>` if needed. No DB or external integration changes.
