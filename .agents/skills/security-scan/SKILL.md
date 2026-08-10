---
name: security-scan
description: Scan dependencies and CI for vulnerabilities. Use when auditing security posture of the repo or dependencies.
---

# Security Scan

## Purpose

Find known vulnerabilities in dependencies and risky CI/configuration patterns.

## When to use

- User requests a security review or dependency audit
- Before release or after major dependency bumps

## Key commands and paths

```bash
# Dependency audit (if available in environment)
pip audit
# or review requirements lockfiles
cat requirements.txt requirements-dev.txt

# Pre-commit / CI security hooks
make pre-commit

# Container and secrets hygiene
grep -r "TOKEN\|PASSWORD\|SECRET" .env.example container/.env.container
```

## Checklist

1. No real credentials in committed files (`.env`, tokens in code).
2. GitHub Actions pinned to SHAs ([AGENTS.md](../../../AGENTS.md) Infrastructure Agent).
3. `AAP_TOKEN_ENCRYPTION_KEY` documented for production ([container/README.md](../../../container/README.md)).
4. Credentials never logged or stored in migration state ([AGENTS.md](../../../AGENTS.md) Client Agent).

## References

- [AGENTS.md](../../../AGENTS.md)
- [container/README.md](../../../container/README.md) — Security Best Practices
- [.pre-commit-config.yaml](../../../.pre-commit-config.yaml)
