"""CLI command to start the FastAPI web server."""

import os

import click


@click.command("serve")
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option("--reload", "do_reload", is_flag=True, help="Enable auto-reload for development")
def serve(host: str, port: int, do_reload: bool) -> None:
    """Start the AAP Bridge web API server.

    Requires the [api] extra: pip install '.[api]'
    """
    try:
        import uvicorn
    except ImportError:
        click.echo(
            "uvicorn is not installed. Install API dependencies with:\n"
            "  pip install '.[api]'\n"
            "or:\n"
            "  uv pip install '.[api]'"
        )
        raise SystemExit(1) from None

    db_url = os.environ.get("MIGRATION_STATE_DB_PATH")
    if not db_url:
        db_url = "sqlite:///aap_bridge.db"
        click.echo(f"MIGRATION_STATE_DB_PATH not set, defaulting to {db_url}")
        os.environ["MIGRATION_STATE_DB_PATH"] = db_url

    click.echo(f"Starting AAP Bridge API on {host}:{port}")

    if do_reload:
        uvicorn.run(
            "aap_migration.api.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=["src"],
        )
    else:
        from aap_migration.api.app import create_app

        app = create_app(db_url=db_url)
        uvicorn.run(app, host=host, port=port)
