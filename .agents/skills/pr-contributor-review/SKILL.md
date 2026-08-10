---
name: pr-contributor-review
description: Review external contributor PRs. Use when triaging community submissions for scope, correctness, and project standards.
---

# PR Contributor Review

## Purpose

Review external PRs against AAP Bridge mission, architectural invariants, and quality gates.

## When to use

- Reviewing a PR from a new or external contributor
- Deciding accept/revise/close on community changes

## Checklist

1. **Scope litmus** ([AGENTS.md](../../../AGENTS.md) Product Focus) — does it serve export/transform/import/validate/report for AAP migration?
2. **Invariants** — dependency order, deferred credential patching, PostgreSQL state DB, ETL disk boundaries, no business logic in CLI/API routes.
3. **Tests** — meaningful coverage; `make check` or CI equivalent must pass.
4. **No scope creep** — reject general Ansible tooling, UI features unrelated to migration.
5. **Docs** — contributor-facing changes need minimal, accurate updates only when behavior changes.

## Commands

```bash
gh pr checkout <number>
make check          # or make c-check for container parity
gh pr review <number> --comment -b "..."
```

## References

- [AGENTS.md](../../../AGENTS.md)
- [Makefile](../../../Makefile)
