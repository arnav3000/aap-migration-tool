# Pre-commit Hook Setup

This repository uses [pre-commit](https://pre-commit.com/) to run automated checks before each commit, including secret detection with gitleaks.

## Quick Setup

### 1. Install pre-commit

```bash
# Using pip
pip install pre-commit

# Or using homebrew (macOS)
brew install pre-commit

# Or using apt (Ubuntu/Debian)
sudo apt install pre-commit
```

### 2. Install the git hooks

```bash
# In the repository root
pre-commit install
```

### 3. Test it works

```bash
# Run all hooks on all files
pre-commit run --all-files

# Run only gitleaks
pre-commit run gitleaks --all-files
```

## What Gets Checked

### Security Checks

- **Gitleaks**: Scans for secrets, API tokens, credentials
- **Private key detection**: Catches SSH private keys
- **Safety check**: Scans Python dependencies for vulnerabilities

### Code Quality

- **Ruff**: Python linting and formatting
- **Bandit**: Python security linting
- **Mypy**: Type checking

### General Checks

- Trailing whitespace
- End-of-file fixer
- Large file detection (>1MB)
- YAML/JSON/TOML validation

## How It Works

When you run `git commit`:

1. Pre-commit automatically runs all configured hooks
2. If any hook fails, the commit is blocked
3. Fix the issues and try again

## Manual Usage

```bash
# Run all hooks on staged files
pre-commit run

# Run all hooks on all files
pre-commit run --all-files

# Run specific hook
pre-commit run gitleaks --all-files

# Skip hooks (use sparingly!)
git commit --no-verify
```

## Gitleaks Configuration

Gitleaks uses `.gitleaks.toml` for configuration:

- **Allowlisted patterns**: Placeholders like `<your-token>`, `***REMOVED***`
- **Allowlisted paths**: Template files, test data, documentation
- **Custom rules**: AAP-specific token patterns

### Common False Positives

If gitleaks flags something that's not a real secret:

**Option 1: Add to allowlist in `.gitleaks.toml`**

```toml
[allowlist]
regexes = [
    '''your-pattern-here''',
]
```

**Option 2: Skip for specific commit (not recommended)**

```bash
git commit --no-verify
```

## Updating Hooks

```bash
# Update all hooks to latest versions
pre-commit autoupdate

# Update specific hook
pre-commit autoupdate --repo https://github.com/gitleaks/gitleaks
```

## CI/CD Integration

Pre-commit hooks run locally before commits. The repository also has GitHub Actions that run gitleaks on every push/PR for additional protection.

**Local (pre-commit):**

- Fast feedback
- Catches issues before commit
- Uses `.gitleaks.toml` config

**CI/CD (GitHub Actions):**

- Comprehensive scan (full history)
- Catches issues in PRs
- Uses same `.gitleaks.toml` config

## Troubleshooting

### Hooks are slow

```bash
# Only run on changed files
git add <files>
pre-commit run

# Skip certain hooks locally
SKIP=mypy,bandit pre-commit run
```

### Hook installation fails

```bash
# Clean and reinstall
pre-commit clean
pre-commit install
```

### Gitleaks false positive

1. Verify it's actually safe (not a real secret)
2. Add pattern to `.gitleaks.toml` allowlist
3. Commit the updated config

## More Information

- [Pre-commit documentation](https://pre-commit.com/)
- [Gitleaks documentation](https://github.com/gitleaks/gitleaks)
- [Security Policy](SECURITY.md)
