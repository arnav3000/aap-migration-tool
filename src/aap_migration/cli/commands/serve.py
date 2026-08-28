"""Serve command — launch the REST API server."""

from __future__ import annotations

import click

from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@click.command(name="serve")
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind")
@click.option("--port", default=8000, show_default=True, type=int, help="Port to bind")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload (dev)")
@click.option(
    "--workers", default=1, show_default=True, type=int, help="Number of workers (use 1 for sqlite)"
)
@click.option(
    "--log-level",
    default="info",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
    help="Uvicorn log level",
)
def serve(host: str, port: int, reload: bool, workers: int, log_level: str) -> None:
    """Launch the AAP Bridge REST API server.

    Examples:

        \b
        # Start on default port 8000
        aap-bridge serve

        \b
        # Custom host/port
        aap-bridge serve --host 127.0.0.1 --port 9000

        \b
        # Development with auto-reload
        aap-bridge serve --reload
    """
    try:
        import uvicorn
    except ImportError:
        click.echo("uvicorn not installed. Install with: pip install 'aap-bridge[api]'", err=True)
        raise click.ClickException("Missing API dependencies") from None

    # For sqlite, force single worker to avoid file lock issues
    if workers != 1:
        logger.warning("workers > 1 with sqlite may cause locks; prefer 1 for api_state.db")

    click.echo(f"Starting AAP Bridge API on http://{host}:{port} (docs: http://{host}:{port}/docs)")
    click.echo(
        f"API prefix: /api  |  Auth: {'enabled (AAP_API_TOKEN set)' if __import__('os').environ.get('AAP_API_TOKEN') else 'disabled (set AAP_API_TOKEN to enable)'}"
    )

    uvicorn.run(
        "aap_migration.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        log_level=log_level,
    )
