# Security Policy

## Supported Versions

We release patches for security vulnerabilities for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability in AAP Bridge, please report it by emailing:

**aap.bridge.183yy@simplelogin.com**

Please include the following information:

1. **Type of vulnerability** (e.g., SQL injection, authentication bypass, credential exposure)
2. **Affected component** (e.g., credential migration, API client, transformation layer)
3. **Steps to reproduce** the vulnerability
4. **Potential impact** of the vulnerability
5. **Suggested fix** (if you have one)

### What to expect

- **Acknowledgment**: We will acknowledge receipt of your vulnerability report within 48 hours
- **Assessment**: We will assess the vulnerability and determine its severity within 5 business days
- **Fix timeline**:
  - Critical vulnerabilities: Patch within 7 days
  - High severity: Patch within 14 days
  - Medium/Low severity: Patch in next regular release
- **Disclosure**: We will coordinate disclosure with you once a fix is available

### Security best practices when using AAP Bridge

1. **Credentials Management**:
   - Never commit `.env` files or credentials to version control
   - Use environment variables or secure vaults (HashiCorp Vault) for credential storage
   - Rotate API tokens regularly
   - Use read-only tokens for export operations when possible

2. **Network Security**:
   - Always use HTTPS/TLS for AAP API connections
   - Verify SSL certificates (avoid `--insecure` in production)
   - Use network segmentation to isolate migration traffic

3. **Database Security**:
   - Protect the `migration_state.db` file (contains ID mappings)
   - Set appropriate file permissions (600 or 640)
   - Consider encrypting the database at rest
   - Back up database securely

4. **Audit and Logging**:
   - Enable structured logging to track all migration operations
   - Monitor logs for suspicious activity
   - Retain logs for compliance and forensic purposes

5. **Access Control**:
   - Use least-privilege principles for AAP service accounts
   - Separate source (read-only) and target (write) credentials
   - Implement role-based access control

6. **Secret Scanning**:
   - This repository uses Gitleaks for automatic secret detection
   - Install pre-commit hooks to prevent accidental credential commits
   - Run `pre-commit install` after cloning the repository

## Security Features

AAP Bridge implements several security features:

- **Credential Encryption**: Sensitive data is handled securely during migration
- **Secret Detection**: Automated scanning with Gitleaks prevents credential leaks
- **Dependency Scanning**: Regular vulnerability checks with pip-audit and Dependabot
- **SAST**: Static analysis with Bandit and Semgrep detects security issues
- **SBOM**: Software Bill of Materials (CycloneDX) for supply chain transparency
- **Audit Trail**: All operations logged via structlog with correlation IDs

## Security Scanning

This project uses automated security scanning:

- **Gitleaks**: Secret detection in code and git history
- **Bandit**: Python security linting
- **Semgrep**: Pattern-based security analysis
- **pip-audit**: Dependency vulnerability scanning
- **Trivy**: Filesystem and dependency scanning
- **Dependabot**: Automated dependency updates

All scans run automatically on every push and pull request.

## Security Updates

Security updates will be released as patch versions and communicated through:

1. GitHub Security Advisories
2. Release notes
3. Email notifications to reporters

## Bug Bounty Program

We do not currently have a bug bounty program. However, we greatly appreciate responsible disclosure and will publicly acknowledge your contribution (with your permission).

## Contact

For security-related questions or concerns:

- **Email**: aap.bridge.183yy@simplelogin.com
- **GitHub**: Create a security advisory (https://github.com/arnav3000/aap-bridge-fork/security/advisories/new)

---

**Last Updated**: 2026-03-30
