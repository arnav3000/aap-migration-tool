# AAP Migration Tool

A production-grade Python tool for migrating Ansible Automation Platform (AAP)
installations between versions, with CLI, Web UI, and dependency analysis.

## Supported migration paths

| Source | Target | Status |
|--------|--------|--------|
| AAP 2.4 (RPM) | AAP 2.5 / 2.6 (containerized) | Tested |
| AAP 2.5 | AAP 2.6 | Tested |

## Features

- **CLI + TUI** -- command-line and interactive text-based interfaces for scripted or guided migrations
- **Web UI** -- React / PatternFly dashboard for connection management, dependency analysis, migration planning, and job tracking
- **Dependency Analyzer** -- builds an inter-organization dependency graph, detects circular dependencies, and groups orgs into migration phases
- **Migration Planner** -- multi-source phased plans with drag-and-drop phase editing and per-phase execution
- **Selective Migration** -- migrate individual orgs or append a suffix to all object names when consolidating multiple AAP instances
- **State Management** -- SQLite or PostgreSQL-backed checkpoints, ID mappings, and job persistence with resume capability
- **Bulk Operations** -- leverages AAP bulk APIs for high-throughput host and inventory imports
- **Credential-First Workflow** -- compares and migrates credentials before other resources to avoid downstream failures

## Quick start

| Method | Guide |
|--------|-------|
| Local Python install | [docs/quickstart-local.md](docs/quickstart-local.md) |
| Containerized CLI | [docs/quickstart-container-cli.md](docs/quickstart-container-cli.md) |
| Web UI (Compose) | [docs/quickstart-web.md](docs/quickstart-web.md) |

## Documentation

- [Architecture overview](docs/architecture.md)
- [Changelog](CHANGELOG.md)
- [Container deployment details](container/README.md)

Build the docs site locally with MkDocs:

```bash
pip install -e ".[docs]"
mkdocs serve
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linters and type checks
pre-commit run --all-files

# Run tests
pytest
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Open a Pull Request

## License

GNU General Public License v3.0 -- see [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting.

## Support

- [GitHub Issues](https://github.com/arnav3000/aap-migration-tool/issues)
