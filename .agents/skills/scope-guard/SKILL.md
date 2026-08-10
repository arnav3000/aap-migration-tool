---
name: scope-guard
description: Evaluate proposed features against project mission. Use before adding features or when asked if something belongs in AAP Bridge.
---

# Scope Guard

## Purpose

Reject or reshape work that does not directly support AAP migration.

## When to use

- User or agent proposes a new feature, module, or large refactor
- Unsure whether a change belongs in this repo

## Litmus test (all three must be **yes**)

1. Does this help **export, transform, import, validate, or report** on AAP resources?
2. Does this fix a **real migration failure or gap**?
3. Would removing this leave users **unable to complete a migration**?

If all three are **no**, the feature does not belong here.

## Out of scope examples

- General-purpose Ansible tooling
- AAP management UI unrelated to migration
- Analytics platforms
- SQLite-first production designs (PostgreSQL is the state DB; SQLite is unit tests only)

## References

- [AGENTS.md](../../../AGENTS.md) — Product Focus and invariant 11 (No scope creep)
