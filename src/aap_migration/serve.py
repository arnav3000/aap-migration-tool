"""Console entry point for the AAP Bridge API server."""

from __future__ import annotations

import argparse
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aap-bridge",
        description="Start the AAP Bridge web API server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", default=8000, type=int, help="Bind port")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Start the FastAPI web API server."""
    args = _build_parser().parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install API dependencies with:\n"
            "  pip install '.[api]'\n"
            "or:\n"
            "  uv pip install '.[api]'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    from aap_migration.api.dependencies import get_db_url

    db_url = get_db_url()
    os.environ.setdefault("MIGRATION_STATE_DB_PATH", db_url)
    print(f"Using database: {db_url}")
    print(f"Starting AAP Bridge API on {args.host}:{args.port}")

    if args.reload:
        uvicorn.run(
            "aap_migration.api.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=["src"],
        )
    else:
        from aap_migration.api.app import create_app

        app = create_app(db_url=db_url)
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
