# Publication Gate — Pre-Public Checklist

Per Plan 004 WI-4.2 and blueprint §3 (Public after sanitization).

This checklist must be completed **before** the agent-wake repository flips
from private to public. History scrubbing is irreversible — do it right once.

## 1. Reconcile git state

- [ ] Working tree is clean (`git status` shows no uncommitted changes)
- [ ] `main` is pushed (`git log origin/main..main` is empty)
- [ ] All branches are merged or deleted
- [ ] No stashes that contain work-domain identifiers

## 2. History scrub (filter-repo)

Use `git-filter-repo` (not the deprecated `git filter-branch`):

```bash
pip install git-filter-repo

# Dry run: see what would change
git filter-repo --dry-run --replace-text <(echo '
# Work-domain identifiers — replace with generic terms
companyname==>COMPANY
projectname==>PROJECT
# Add patterns as needed:
# regex:realname==>ANALYST
# regex:hostname\.internal==>HOSTNAME
')

# Real run (creates a backup in .git/filter-repo/)
git filter-repo --replace-text replacements.txt
```

Check for:
- [ ] Work-domain email addresses in commit messages (`git log --format='%ae' | sort -u`)
- [ ] Work-domain hostnames in code, configs, and examples
- [ ] Internal project codenames in commit messages or file contents
- [ ] Real person names (replace with generic terms or GitHub handles)
- [ ] Internal IP addresses or network topology hints
- [ ] Internal tool/service names specific to the workplace

## 3. CI identifier gate

Add a CI job that fails if work-domain identifiers appear in new commits:

```yaml
# .github/workflows/identifier-gate.yml
name: Identifier Gate
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Check for work-domain identifiers
        run: |
          # Add patterns as needed
          PATTERNS="COMPANY_NAME|internal\.hostname|codename"
          if git log --all | grep -iE "$PATTERNS"; then
            echo "Work-domain identifiers found in history"
            exit 1
          fi
```

- [ ] Identifier gate CI job is green on the scrubbed history
- [ ] Gate patterns cover all known identifiers from step 2

## 4. Secret scan

- [ ] No committed secrets (`git-secrets --scan`, `trufflehog filesystem .`)
- [ ] No API keys, tokens, or passwords in code or examples
- [ ] `.env` files are in `.gitignore`
- [ ] Example config files use placeholder values only

## 5. Repo hygiene

- [ ] `README.md` is clean (no internal references, working install instructions)
- [ ] `LICENSE` is MIT with no attribution to internal entities
- [ ] `.github/` workflows don't reference internal infrastructure
- [ ] No internal document links (wiki, Jira, Confluence) in code or docs
- [ ] `design/` directory reviewed — historical docs may reference internal context
- [ ] `reflections/` directory reviewed — session reflections may reference internal context

## 6. Publication

- [ ] Repository visibility flipped to public
- [ ] GitHub repo settings: issues enabled, wiki disabled (if not used)
- [ ] First public clone test: `git clone` from a clean machine, follow README
- [ ] `agent-wake doctor --json` runs clean on the fresh clone
- [ ] `agent-wake install-harness claude --dry-run` works on the fresh clone

## Post-publication

- [ ] Update cross-references in regista, agent-suite, and sibling repos to point to public URL
- [ ] Add to `SUITE.lock` (if suite is deployed)
- [ ] Announce if appropriate
