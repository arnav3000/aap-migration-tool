# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.5.x | Yes |
| 0.4.x | Yes |
| < 0.4 | No |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability, please report it by emailing:

**<aap.bridge.183yy@simplelogin.com>**

Include the following information:

1. **Type of vulnerability** (e.g., SQL injection, authentication bypass, credential exposure)
2. **Affected component** (e.g., credential migration, API client, transformation layer)
3. **Steps to reproduce**
4. **Potential impact**
5. **Suggested fix** (if you have one)

### What to expect

- **Acknowledgment**: Within 48 hours
- **Assessment**: Severity determined within 5 business days
- **Fix timeline**:
  - Critical: patch within 7 days
  - High: patch within 14 days
  - Medium/Low: next regular release
- **Disclosure**: Coordinated with you once a fix is available

### Security best practices when using AAP Migration Tool

1. **Credentials**: Never commit `.env` files. Use environment variables or HashiCorp Vault. Rotate tokens regularly.
2. **Network**: Use HTTPS/TLS. Verify SSL certificates in production.
3. **Database**: Protect `migration_state.db` (permissions 600/640). Consider encryption at rest.
4. **Logging**: Enable structured logging. Retain logs for audit.
5. **Access control**: Use least-privilege AAP service accounts. Separate read-only (source) and write (target) tokens.
6. **Secret scanning**: Pre-commit hooks with Gitleaks are configured. Run `pre-commit install` after cloning.

## Security Features

- Encrypted token storage in the API database (Fernet)
- Gitleaks secret detection in pre-commit
- Bandit static analysis
- Structured logging with automatic sensitive-data redaction

## Contact

- **Email**: <aap.bridge.183yy@simplelogin.com>
- **GitHub**: [Security Advisories](https://github.com/arnav3000/aap-migration-tool/security/advisories/new)
