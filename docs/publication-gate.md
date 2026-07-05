# Publication Gate — Pre-Public Checklist

Per Plan 004 WI-4.2 and blueprint §3 (Public after sanitization).

This checklist must be completed **before** the agent-wake repository flips
from private to public. History scrubbing is irreversible — do it right once.

> **Status (2026-07-05):** Denylist patterns configured from adcs-lens +
> gpo-lens samples (covering CA CN and all identifier forms per the WI-010
> lesson). `git filter-repo --dry-run` audit completed — **zero leaks found**
> across all 38 commits, all branches, and the v0.0.1 tag. Sections 1–5
> verified; section 6 (publication flip) and post-publication remain
> owner-gated. **No destructive scrub or visibility flip has been run** —
> this is dry-run + report only, per instructions.
>
> The literal identifier values are NOT repeated here — they live only in
> the gitignored `.identifiers-denylist.local` file. Describing them by
> category (not by value) prevents the checklist from flagging itself in
> the identifier gate.

## 1. Reconcile git state

- [x] Working tree is clean (`git status` shows no uncommitted changes)
- [x] `main` is pushed (`git log origin/main..main` is empty)
- [ ] All branches are merged or deleted (5 feature branches + 1 rename
      branch exist; they should be merged or deleted before the flip)
- [x] No stashes that contain work-domain identifiers

## 2. History scrub (filter-repo) — dry-run audit

Use `git-filter-repo` (not the deprecated `git filter-branch`):

```bash
pip install git-filter-repo

# Dry run: see what would change
git filter-repo --dry-run --replace-text replacements.txt --force

# Real run (creates a backup in .git/filter-repo/) — NOT YET RUN
git filter-repo --replace-text replacements.txt
```

The `replacements.txt` file is generated from `.identifiers-denylist.local`
by appending `==>REDACTED` to each non-comment line. Do not commit the
generated file (it contains raw identifier values).

**Audit result (2026-07-05):** A `git filter-repo --dry-run --replace-text`
was run on a fresh `--no-local` clone with all branches fetched. The
replacement file covered all 17 identifier forms extracted from adcs-lens +
gpo-lens samples (see §3 below). The diff between
`.git/filter-repo/fast-export.original` and `fast-export.filtered` showed
**zero content changes** — the only differences were `original-oid` marker
lines stripped by filter-repo (a dry-run artifact, not a content change).

A direct scan of all blobs across all refs (`git rev-list --all --objects`)
and all commit messages (`git log --all --format='%B'`) for every denylisted
identifier also returned **zero matches**.

**Conclusion:** No history scrub is needed. The repository has never
contained any of the work-domain identifiers from the denylist. The
filter-repo real run and the delete-and-recreate remedy (the WI-010 lesson
from adcs-lens, where pushed PR refs survive force-push) are not required.

Check for:
- [x] Work-domain email addresses in commit messages — only the public
      GitHub author email (intentionally retained per adcs-lens precedent)
- [x] Work-domain hostnames in code, configs, and examples — none found
- [x] Internal project codenames in commit messages or file contents — none
- [x] Real person names — the public author name is retained; no other real
      names found
- [x] Internal IP addresses or network topology hints — none found
- [x] Internal tool/service names specific to the workplace — none found

## 3. Identifier gate (implemented — gpo-lens pattern)

The gate is `scripts/check_committed_identifiers.py`, borrowed from gpo-lens. The
denylist is **never committed**: it comes from `AGENT_WAKE_FORBIDDEN_IDENTIFIERS`
(whitespace-separated), resolved from the gitignored `.identifiers-denylist.local`
locally and the repository secret of the same name in CI. The committed script
carries no identifiers. It scans tracked files (CI) or staged files (the
pre-commit hook), is UTF-16/BOM-aware, and exits 0 when the denylist is unset so
a fresh clone is not bricked.

**Denylist patterns configured (2026-07-05):** 17 identifiers covering all
forms extracted from adcs-lens/samples/ and gpo-lens/samples/, organized by
the WI-010 lesson categories. The literal values are NOT listed here (see
the gitignored `.identifiers-denylist.local` for the exact tokens). The
categories covered are:

- AD domain name (DNS form) — from gpo-lens + adcs-lens samples
- AD domain name (NetBIOS form) — from gpo-lens principals/group-members
- CA common name (the WI-010 gap — CA CN, not just hostname) — from adcs-lens
- CA hostname (short form) — from adcs-lens collector-manifest
- Infrastructure hostnames (5 tokens) — from gpo-lens denylist
- Lab / work domain (2 tokens: short + FQDN) — from gpo-lens calibration
- Service account name — from gpo-lens denylist
- Real domain SID (the work-domain SID base, not well-known SIDs) —
  from gpo-lens + adcs-lens
- Org-specific OID arc (enterprise PKI sub-arc) — from adcs-lens oid-objects
- Org-specific CA template name (no-space form) — from adcs-lens templates
- Real person username (a Domain Admin on the org-specific template) — from
  adcs-lens realrun/templates.json
- AD distinguished name form (DC=... DN) — from adcs-lens collector-manifest

The display-name form of the CA template (with spaces) cannot be
denylisted because the parser splits on whitespace and the individual words
are too generic. The no-space form is sufficient — any file referencing the
template by its `name` field (the programmatic form) will be caught.

The public GitHub identity (author handle, repo URL) is intentionally NOT
in the denylist — it is the published identity, per the adcs-lens
publication-review precedent. Only the AD-domain FQDN is forbidden, not the
bare email domain (so the author email is not flagged).

**Known limitations:**
- Multi-word identifiers (with spaces) cannot be denylisted due to the
  whitespace-splitting parser. The no-space forms are used instead.
- Substring matching (not word-boundary) is an intentional trade-off: it
  catches identifiers embedded in longer strings (e.g. FQDNs) but could
  theoretically cause false positives on short tokens. The denylist avoids
  tokens shorter than 5 chars; the lab-domain short name (5 chars) is the
  shortest and is specific enough that false positives are unlikely in this
  repo.

- [ ] `scripts/install-git-hooks.sh` run in this clone (pre-commit early
      warning — catches a leak before it enters history, not after push)
- [x] `.identifiers-denylist.local` lists the forbidden identifiers (17
      tokens covering all forms from adcs-lens + gpo-lens samples); it is
      gitignored
- [ ] `AGENT_WAKE_FORBIDDEN_IDENTIFIERS` repository secret is set in CI
      (the secret value is the same whitespace-separated token list; the
      owner must set this in GitHub repo settings before the flip)
- [ ] The `identifier-gate` CI job is green on the current tree (the local
      gate passes with the denylist; CI requires the repo secret to be set)
- [x] Local gate verified: `AGENT_WAKE_FORBIDDEN_IDENTIFIERS="$(cat
      .identifiers-denylist.local)" python scripts/check_committed_identifiers.py`
      exits 0 with zero violations on the current tracked tree

## 4. Secret scan

- [x] No committed secrets (manual scan for API keys, tokens, private keys,
      AWS credentials — none found; `git-secrets`/`trufflehog` not installed
      but pattern-based grep covered the common secret formats)
- [x] No API keys, tokens, or passwords in code or examples
- [x] `.env` files are in `.gitignore` (`.env` and `.env.*` gitignored,
      `!.env.example` negation allows the example template)
- [x] Example config files use placeholder values only

## 5. Repo hygiene

- [x] `README.md` is clean (no internal references, working install
      instructions)
- [x] `LICENSE` is MIT with no attribution to internal entities
- [x] `.github/` workflows don't reference internal infrastructure
- [x] No internal document links (wiki, Jira, Confluence) in code or docs
- [x] `design/` directory reviewed — no work-domain identifiers found
- [x] `reflections/` directory reviewed — no work-domain identifiers found

## 6. Publication

> **Not yet executed.** These items are owner-gated and must be done
> after the denylist CI secret is set and the feature branches are
> reconciled.

- [ ] Repository visibility flipped to public
- [ ] GitHub repo settings: issues enabled, wiki disabled (if not used)
- [ ] First public clone test: `git clone` from a clean machine, follow README
- [ ] `agent-wake doctor --json` runs clean on the fresh clone
- [ ] `agent-wake install-harness claude --dry-run` works on the fresh clone

## Post-publication

- [ ] Update cross-references in regista, agent-suite, and sibling repos to point to public URL
- [ ] Add to `SUITE.lock` (if suite is deployed)
- [ ] Announce if appropriate