---
name: lean-ci
description: CI workflow guidance for minimal, reproducible checks. Use when changing GitHub Actions or deciding what CI to run.
---

# Lean CI

## Purpose

Keep CI fast and reproducible: run the smallest check set that gates quality, mirror it locally via Make.

## When to use

- Adding or editing `.github/workflows/`
- Deciding what to run on PR vs merge
- Debugging CI failures without running the full matrix locally

## Key paths and commands

| Goal | Command |
|------|---------|
| Local pre-commit gate | `make check` |
| Containerized PR gate | `make c-check` |
| Full regression + coverage | `make c-ci-full` |
| Backend tests only | `make test-unit` or `make c-test-backend` |

**Do not** invoke `pytest`, `ruff`, `black`, or `mypy` directly — use the Make targets in [AGENTS.md](../../../AGENTS.md) Quality Gates.

## CI principles

1. Pin GitHub Actions to commit SHAs (see [AGENTS.md](../../../AGENTS.md) Infrastructure Agent).
2. Every CI step must be reproducible locally via `make <target>`.
3. Keep integration/`requires_aap` tests behind markers — opt-in only.
4. Prefer `make c-check` over ad-hoc workflow steps.

## References

- [AGENTS.md](../../../AGENTS.md)
- [.github/workflows/](../../../.github/workflows/)
- [Makefile](../../../Makefile)
