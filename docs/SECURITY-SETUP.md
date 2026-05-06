# Security Setup Guide

This guide explains how to set up and use the security scanning tools in the AAP Bridge project.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Pre-commit Hooks](#pre-commit-hooks)
3. [GitHub Actions](#github-actions)
4. [Manual Security Scans](#manual-security-scans)
5. [Tool Configuration](#tool-configuration)
6. [Troubleshooting](#troubleshooting)

## Quick Start

### Install Security Tools Locally

```bash
# Activate virtual environment
source .venv/bin/activate

# Install development dependencies (includes security tools)
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Run all security scans
pre-commit run --all-files
```

## Pre-commit Hooks

Pre-commit hooks automatically run security checks before each commit.

### Installation

```bash
pip install pre-commit
pre-commit install
```

### What Gets Checked

Every commit is automatically scanned for:

- **Secrets**: Gitleaks detects hardcoded credentials, API tokens, private keys
- **Security Issues**: Bandit finds common Python security vulnerabilities
- **Code Quality**: Ruff lints and formats code
- **Type Safety**: mypy performs static type checking
- **Common Mistakes**: Trailing whitespace, large files, merge conflicts, etc.

### Usage

```bash
# Automatically runs on git commit
git commit -m "your message"

# Manually run all hooks
pre-commit run --all-files

# Run specific hook
pre-commit run gitleaks --all-files
pre-commit run bandit --all-files
pre-commit run ruff --all-files

# Update hooks to latest versions
pre-commit autoupdate

# Skip hooks (not recommended for main branch)
git commit --no-verify -m "emergency fix"
```

### Bypassing Hooks

If you need to bypass pre-commit hooks temporarily:

```bash
# Skip all hooks (use with caution)
git commit --no-verify -m "your message"

# Skip specific files (add to .gitignore or pre-commit config)
```

## GitHub Actions

Security scans run automatically on every push and pull request.

### Workflow: `security-scan.yml`

Located at `.github/workflows/security-scan.yml`

**Triggers**:
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`
- Weekly schedule (Sundays at midnight UTC)
- Manual trigger (workflow_dispatch)

**Jobs**:

1. **Dependency Scan** (pip-audit)
   - Scans `requirements.txt` for vulnerable packages
   - Reports known CVEs
   - Fails on HIGH/CRITICAL vulnerabilities

2. **Secret Detection** (Gitleaks)
   - Scans entire git history
   - Detects 140+ secret types
   - Blocks commits containing credentials

3. **SAST - Bandit**
   - Python security linting
   - Finds SQL injection, command injection, etc.
   - Configurable severity thresholds

4. **SAST - Semgrep**
   - Pattern-based security analysis
   - Custom rules for AAP-specific issues
   - Broad vulnerability coverage

5. **Code Quality** (Ruff)
   - Fast Python linting
   - Formatting validation
   - PEP 8 compliance

6. **License Compliance**
   - Checks all dependency licenses
   - Ensures GPL compatibility
   - Generates license reports

7. **SBOM Generation**
   - Creates Software Bill of Materials
   - CycloneDX format (JSON/XML)
   - Supply chain transparency

8. **Filesystem Scan** (Trivy)
   - Vulnerability scanning
   - Uploads results to GitHub Security
   - SARIF format for integration

### Viewing Results

**GitHub UI**:
1. Go to repository → Actions tab
2. Select "Security & Quality Scan" workflow
3. View job results and artifacts

**Security Tab**:
1. Repository → Security → Code scanning alerts
2. View Trivy and Semgrep findings
3. Triage and dismiss false positives

**Artifacts**:
- Download scan reports from workflow run
- Available for 30 days (90 days for SBOM/licenses)

## Manual Security Scans

Run security scans locally before pushing:

### 1. Secret Detection (Gitleaks)

```bash
# Install gitleaks
brew install gitleaks  # macOS
# or download from https://github.com/gitleaks/gitleaks/releases

# Scan current state
gitleaks detect --source . --verbose

# Scan git history
gitleaks detect --source . --log-opts="--all" --verbose

# Scan with custom config
gitleaks detect --config .gitleaks.toml --source . --verbose

# Generate report
gitleaks detect --source . --report-path gitleaks-report.json --report-format json
```

### 2. Dependency Vulnerabilities (pip-audit)

```bash
# Install pip-audit
pip install pip-audit

# Scan installed packages
pip-audit

# Scan requirements.txt
pip-audit --requirement requirements.txt

# Fix vulnerabilities automatically
pip-audit --fix --requirement requirements.txt

# Generate report
pip-audit --format json --output pip-audit-report.json
```

### 3. Security Linting (Bandit)

```bash
# Install bandit
pip install bandit[toml]

# Scan source code
bandit -r src/

# Use configuration from pyproject.toml
bandit -r src/ -c pyproject.toml

# Generate detailed report
bandit -r src/ -f json -o bandit-report.json
bandit -r src/ -f html -o bandit-report.html

# Scan specific severity
bandit -r src/ --severity-level medium
```

### 4. Pattern-based Analysis (Semgrep)

```bash
# Install semgrep
pip install semgrep

# Run auto-config (recommended rules)
semgrep scan --config auto

# Scan with specific rulesets
semgrep scan --config "p/python"
semgrep scan --config "p/security-audit"
semgrep scan --config "p/owasp-top-ten"

# Generate report
semgrep scan --config auto --json --output semgrep-report.json
```

### 5. Filesystem Scan (Trivy)

```bash
# Install trivy
brew install aquasecurity/trivy/trivy  # macOS

# Scan filesystem
trivy fs .

# Scan with severity filter
trivy fs --severity HIGH,CRITICAL .

# Generate report
trivy fs --format json --output trivy-report.json .
trivy fs --format sarif --output trivy-report.sarif .
```

### 6. License Compliance

```bash
# Install pip-licenses
pip install pip-licenses

# List all licenses
pip-licenses

# Generate markdown report
pip-licenses --format=markdown --output-file=licenses.md

# Check for specific licenses
pip-licenses --format=plain-vertical | grep -i "gpl\|apache\|mit"
```

## Tool Configuration

### Gitleaks Configuration (`.gitleaks.toml`)

```toml
# Custom patterns for AAP-specific secrets
[[rules]]
id = "aap-token"
description = "Ansible Automation Platform API Token"
regex = '''(?i)(aap|awx|controller)[_-]?token['\"]?\s*[:=]\s*['\"]?[a-zA-Z0-9]{40,}'''

# Allowlist for false positives
[allowlist]
paths = [
    '''tests/fixtures/.*''',
    '''docs/.*\.md''',
]
```

### Bandit Configuration (`pyproject.toml`)

```toml
[tool.bandit]
exclude_dirs = ["tests", ".venv"]
skips = ["B101"]  # Allow assert in tests
severity = "MEDIUM"
```

### Ruff Configuration (`pyproject.toml`)

```toml
[tool.ruff.lint]
select = [
    "S",  # flake8-bandit (security)
    "B",  # flake8-bugbear
    # ... other linters
]
```

## Troubleshooting

### False Positives

**Gitleaks**:
```toml
# Add to .gitleaks.toml
[allowlist]
paths = ["path/to/file"]
regexes = ['''pattern-to-ignore''']
```

**Bandit**:
```python
# Inline comment to skip specific line
secret = get_secret()  # nosec B123

# Skip entire function
def test_function():
    # nosec
    pass
```

**Semgrep**:
```python
# Inline comment
result = eval(code)  # nosemgrep: python.lang.security.audit.eval-use
```

### Pre-commit Hook Failures

```bash
# Update hooks
pre-commit autoupdate

# Clear cache
pre-commit clean
pre-commit gc

# Reinstall hooks
pre-commit uninstall
pre-commit install
```

### Dependency Conflicts

```bash
# Create clean virtual environment
python -m venv .venv-clean
source .venv-clean/bin/activate
pip install -r requirements.txt
```

### GitHub Actions Failures

1. Check workflow logs in Actions tab
2. Download artifacts for detailed reports
3. Run same scan locally to reproduce
4. Update configuration if needed

## Best Practices

1. **Always run pre-commit hooks** before pushing
2. **Review security findings** before dismissing as false positives
3. **Update dependencies regularly** (Dependabot PRs)
4. **Never commit secrets** - use environment variables or vaults
5. **Keep security tools updated** (`pre-commit autoupdate`)
6. **Test locally first** before relying on CI/CD
7. **Document exceptions** when skipping security checks

## Additional Resources

- [Gitleaks Documentation](https://github.com/gitleaks/gitleaks)
- [Bandit Documentation](https://bandit.readthedocs.io/)
- [Semgrep Documentation](https://semgrep.dev/docs/)
- [Trivy Documentation](https://aquasecurity.github.io/trivy/)
- [Pre-commit Documentation](https://pre-commit.com/)

---

**Last Updated**: 2026-03-30
