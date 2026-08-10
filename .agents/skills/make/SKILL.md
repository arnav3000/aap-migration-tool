---
name: make
description: Makefile target reference for lint, test, build, and container workflows. Use when running project commands or advising on quality gates.
---

# Make

## Purpose

Single entry point for lint, format, typecheck, test, docs, and container workflows.

## When to use

- Before committing: `make check`
- Before opening a PR: `make c-check` or `make c-ci-full`
- Container deploy or test: `make build`, `make up`, `make c-test-all`

## Common targets

| Target | What it does |
|--------|----------------|
| `make help` | List all targets |
| `make setup` | Dev environment setup |
| `make format` | black + isort |
| `make lint` | ruff |
| `make typecheck` | mypy |
| `make test` | Full pytest suite |
| `make test-unit` | Unit tests only |
| `make check` | format + lint + typecheck + test |
| `make pre-commit` | All pre-commit hooks |
| `make build` / `make up` | Container stack |
| `make c-check` | Containerized lint + typecheck + tests |
| `make c-ci-full` | Full regression (80% coverage gate) |

## Prohibited direct invocations

Use Make instead of raw `pytest`, `ruff`, `black`, `mypy`, or `pre-commit run` — see [AGENTS.md](../../../AGENTS.md) Quality Gates.

## References

- [Makefile](../../../Makefile)
- [AGENTS.md](../../../AGENTS.md)
