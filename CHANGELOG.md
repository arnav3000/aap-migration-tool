# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-04-14

### Added

- **Migration Reporting**
  - New `migration-report` command for comprehensive post-migration analysis
  - Detailed failure tracking with source IDs, names, phases, and error messages
  - Discrepancy detection: identifies resources transformed but not imported
  - Missing resource identification with specific details (ID, name, type)
  - Console summary with color-coded status indicators
  - Markdown report output with tables and statistics
- **Project Failure Analysis**
  - New `analyze-project-failures` command for troubleshooting failed project imports
  - Root cause identification (name collisions, dependency issues, credential mapping failures)
  - Step-by-step manual intervention instructions with API examples
  - SQL snippets for manual ID mapping updates
- **Enhanced Error Handling**
  - Import exceptions now mark resources as "failed" in database
  - Current-run-only failure reporting (excludes historical failures)
  - Automatic migration-report hint displayed when imports fail
- **Documentation Improvements**
  - Containerized deployment featured as primary installation method in README
  - Added guides/ and workflows/ directories to MkDocs navigation
  - 7 user guides and 3 workflow diagrams now accessible from documentation site
- **Security Enhancements**
  - Enhanced pre-commit hooks with AAP-specific secret patterns
  - Additional gitleaks patterns for AAP tokens and credentials

### Fixed

- Migration report discrepancy calculation now excludes failed resources
- Directory-based export structure properly handled in migration report
- Failure tracking now shows only current migration run failures

### Changed

- Repository cleanup: removed 650KB+ of test output and generated files
- Improved `.gitignore` to prevent future test output from being committed
- Enhanced failure notification visibility during imports

## [0.1.0] - 2025-12-05

### Added

- Initial release of AAP Bridge
- **Migration Framework**
  - ETL pipeline for source-to-target AAP migrations
  - Support for all major AAP resource types: organizations, users, teams,
    credentials, credential types, execution environments, projects,
    inventories, hosts, job templates, workflow job templates, and schedules
  - RBAC role assignment migration
  - Bulk API operations for high-performance host and inventory imports
- **State Management**
  - SQLite (default) or PostgreSQL state tracking with checkpoint/resume capability
  - ID mapping persistence for cross-system resource references
  - Idempotent operations to prevent duplicate creation
- **Export/Import Operations**
  - Split-file export for large datasets (configurable records per file)
  - Automatic file discovery and ordered import
  - Metadata tracking for export sessions
- **Validation**
  - Statistical sampling validation (configurable confidence level and margin of
    error)
  - Count reconciliation between source and target
  - Phase-by-phase validation support
- **CLI Interface**
  - `aap-bridge` - Single command with a menu-driven interface
  - `aap-bridge migrate` - Full migration with phase control
  - `aap-bridge export` - Export resources from source AAP
  - `aap-bridge import` - Import resources to target AAP
  - `aap-bridge validate` - Validate migration completeness
  - `aap-bridge state` - View and manage migration state
  - `aap-bridge cleanup` - Clean up target resources or local data
- **Progress Display**
  - Rich-based live progress display with real-time metrics
  - Multiple output modes: normal, quiet, CI/CD, and detailed
  - Rate tracking, success/failure counts, and timing information
- **Logging**
  - Structured logging with structlog
  - Separate console (human-readable) and file (JSON) output
  - Automatic sensitive data redaction
  - Configurable log levels for console and file
- **Configuration**
  - YAML-based configuration with environment variable substitution
  - Resource renaming via mappings.yaml (e.g., credential type name changes
    between versions)
  - Endpoint filtering via ignored_endpoints.yaml
  - Extensive performance tuning options

### Security

- Automatic redaction of sensitive fields in logs (tokens, passwords, SSH keys)
- Environment variable support for all credentials
- No hardcoded secrets in configuration files

[Unreleased]: https://github.com/arnav3000/aap-bridge-fork/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/arnav3000/aap-bridge-fork/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/arnav3000/aap-bridge-fork/releases/tag/v0.1.0
