# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.4] - 2026-05-15

### Added

- **Web UI**
  - React / PatternFly v5 single-page application with full migration workflow
  - Settings page for managing AAP connections with encrypted token storage (Fernet)
  - Dependency Analysis page with org-level dependency graphs, quality scores, and migration phases
  - Migration Planner: multi-source phased plans, drag-and-drop phase editing, per-phase execution
  - Selective org migration and object-name suffix for consolidating multiple AAP instances
  - Object Browser for inspecting resources on a connected AAP instance
  - Cleanup page with confirmation gate
  - Jobs page with live log streaming via WebSocket
  - Global and component-level React error boundaries
- **FastAPI API**
  - REST API layer exposing all CLI functionality over HTTP
  - Background job system (`JobService`) with `asyncio` task management
  - Full job persistence: log lines, structured events, and results stored in DB
  - Sequential job IDs alongside UUIDs for easier identification
  - WebSocket endpoint for real-time log streaming
- **Dependency Analyzer improvements**
  - Cycle-aware phase grouping (`group_into_phases_with_cycles`)
  - Partial topological sort that separates independent orgs from cycle members
  - Circular-dependency detection surfaced in analysis reports
- **Migration Planner backend**
  - Multi-source plan creation from completed analysis scans
  - Phase editing, saving, and per-phase execution with job tracking
- **Documentation**
  - Architecture overview (`docs/architecture.md`)
  - Quick-start guides for local install, containerized CLI, and web UI
  - MkDocs configuration updated for new doc structure

### Changed

- Renamed project from AAP Bridge to AAP Migration Tool across docs and config
- Slimmed README to an overview with links to detailed docs
- Moved `rbac_migration.py` to `scripts/`
- Moved all container files to `container/` directory
- PostgreSQL image switched to `registry.redhat.io/rhel9/postgresql-15:latest`
- Deduplicated pre-commit hooks and pyproject.toml dependencies
- Updated `.gitignore` to track `docs/` and `scripts/`

### Fixed

- Dependency analysis serialization double-wrapping `migration_phases` (caused UI blank screen)
- Foreign-key violation when starting plan-phase execution (synchronous initial job persist)
- Planner rendering corrupted org names from double-wrapped phase data
- Connection test using unauthenticated `/ping/` endpoint (now uses `/me/`)
- HTML report download for dependency analysis
- Various `ruff`, `mypy`, and `bandit` pre-commit findings

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

- Initial release
- **Migration Framework**
  - ETL pipeline for source-to-target AAP migrations
  - Support for all major AAP resource types
  - RBAC role assignment migration
  - Bulk API operations for high-performance imports
- **State Management**
  - SQLite or PostgreSQL state tracking with checkpoint/resume
  - ID mapping persistence
  - Idempotent operations
- **CLI Interface**
  - `aap-migrate` command with TUI and direct CLI modes
  - Commands: migrate, export, import, transform, validate, state, cleanup, credentials
- **Logging and Progress**
  - Rich-based live progress display
  - Structured logging with structlog

### Security

- Automatic redaction of sensitive fields in logs
- Environment variable support for all credentials

[Unreleased]: https://github.com/arnav3000/aap-migration-tool/compare/v0.5.4...HEAD
[0.5.4]: https://github.com/arnav3000/aap-migration-tool/compare/v0.4.0...v0.5.4
[0.4.0]: https://github.com/arnav3000/aap-migration-tool/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/arnav3000/aap-migration-tool/releases/tag/v0.1.0
